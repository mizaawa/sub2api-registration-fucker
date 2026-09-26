#!/usr/bin/env python3
"""Portable registration load tester with per-egress-IP cooldown.

Run `python register_test.py` for the configured (unlimited) target, or
`python register_test.py --limit 10000` for a bounded run. PyYAML is bundled
with the Windows distribution.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

from scheduler import CooldownScheduler, Node


ROOT = Path(__file__).resolve().parent
REGISTER_PATH = "/api/v1/auth/register"
USER_AGENT = "register-loadtest/1.0"
RESULT_FIELDS = (
    "number", "timestamp", "node", "exit_ip", "status", "verdict",
    "latency_ms", "email", "detail",
)


def direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def proxy_opener(port):
    proxy = "http://127.0.0.1:{}".format(port)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def read_config(path):
    with path.open("r", encoding="utf-8-sig") as stream:
        cfg = json.load(stream)
    cfg.setdefault("enable_dynamic_ip_pool", True)
    if not isinstance(cfg["enable_dynamic_ip_pool"], bool):
        raise ValueError("配置 enable_dynamic_ip_pool 必须是布尔值")
    required = ("target_url", "email_domain")
    if cfg["enable_dynamic_ip_pool"]:
        required += ("subscription_url",)
    for key in required:
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError("配置 {} 不能为空".format(key))
    for key in ("target_url", "subscription_url"):
        if key == "subscription_url" and not cfg["enable_dynamic_ip_pool"]:
            continue
        parsed = urllib.parse.urlsplit(cfg[key])
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("配置 {} 不是有效 HTTP(S) 地址".format(key))
    cfg.setdefault("limit", 0)
    cfg.setdefault("quota_per_ip", 5)
    cfg.setdefault("window_seconds", 60)
    cfg.setdefault("timeout_seconds", 15)
    cfg.setdefault("node_health_timeout_ms", 3500)
    cfg.setdefault("invitation_code", "")
    cfg.setdefault("healthcheck_url", cfg["target_url"])
    cfg.setdefault("exit_ip_url", "https://api64.ipify.org?format=json")
    cfg.setdefault("stop_on_verification", True)
    for key in ("limit", "quota_per_ip", "window_seconds",
                "timeout_seconds", "node_health_timeout_ms"):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool):
            raise ValueError("配置 {} 必须是整数".format(key))
    if (cfg["limit"] < 0 or cfg["quota_per_ip"] < 1
            or cfg["window_seconds"] < 1 or cfg["timeout_seconds"] < 1
            or cfg["node_health_timeout_ms"] < 100):
        raise ValueError("配置中的次数和超时时间必须为正数（limit 可为 0）")
    for key in ("healthcheck_url", "exit_ip_url"):
        parsed = urllib.parse.urlsplit(cfg[key])
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("配置 {} 不是有效 HTTP(S) 地址".format(key))
    return cfg


def mihomo_binary():
    override = os.environ.get("MIHOMO_PATH", "").strip()
    filename = "mihomo.exe" if os.name == "nt" else "mihomo"
    candidates = [Path(override)] if override else [
        ROOT / "mihomo" / filename,
        ROOT / "vendor" / filename,
        ROOT / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "未找到 Mihomo 核心；运行 tools/package_windows.py 打包，"
        "或设置 MIHOMO_PATH 指向已安装的核心")


def local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch_subscription(url, destination, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": "mihomo/1.19.31", "Accept": "text/yaml,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl.create_default_context()) as response:
            data = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("订阅下载失败: HTTP {}".format(exc.code)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError("订阅下载失败: {}".format(
            type(exc).__name__)) from None
    if len(data) > 8 * 1024 * 1024 or not data.strip():
        raise RuntimeError("订阅数据为空或超过 8 MiB")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("订阅不是 UTF-8 Clash YAML") from exc
    destination.write_bytes(data)


def prepare_subscription(path):
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            profile = yaml.safe_load(stream)
    except yaml.YAMLError:
        raise RuntimeError("订阅 YAML 解析失败") from None
    if not isinstance(profile, dict):
        raise RuntimeError("订阅不是 Clash 配置")
    proxies = profile.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        raise RuntimeError("订阅没有可用的 proxies 列表")
    if not all(isinstance(proxy, dict) for proxy in proxies):
        raise RuntimeError("订阅 proxies 列表格式不正确")
    provider = yaml.safe_dump({"proxies": proxies}, allow_unicode=True,
                              sort_keys=False)
    path.write_text(provider, encoding="utf-8")
    return profile


def mihomo_config(profile, proxy_port, controller_port, secret, cfg):
    dns = profile.get("dns")
    if dns is not None and not isinstance(dns, dict):
        raise RuntimeError("订阅 DNS 配置格式不正确")
    config = {
        "mixed-port": proxy_port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "external-controller": "127.0.0.1:{}".format(controller_port),
        "secret": secret,
        "ipv6": bool(profile.get("ipv6", False)),
        "profile": {"store-selected": False, "store-fake-ip": False},
        "proxy-providers": {
            "subscription": {
                "type": "file",
                "path": "./provider.yaml",
                "health-check": {
                    "enable": True,
                    "lazy": False,
                    "interval": 120,
                    "timeout": cfg["node_health_timeout_ms"],
                    "url": cfg["healthcheck_url"],
                },
            },
        },
        "proxy-groups": [{"name": "REGISTER_EGRESS", "type": "select",
                          "use": ["subscription"]}],
        "rules": ["MATCH,REGISTER_EGRESS"],
    }
    if dns:
        # The exported profile's listener may already belong to Clash Verge.
        config["dns"] = {key: value for key, value in dns.items()
                         if key != "listen"}
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


class MihomoPool:
    """Isolated, loopback-only Mihomo instance with a private controller."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.workdir = None
        self.process = None
        self.log = None
        self.proxy_port = 0
        self.controller_port = 0
        self.secret = secrets.token_urlsafe(32)
        self.opener = None
        self.nodes = []

    def __enter__(self):
        try:
            self.start()
            return self
        except BaseException:
            self.stop()
            raise

    def __exit__(self, _type, _value, _traceback):
        self.stop()

    def controller(self, path, selected=None):
        url = "http://127.0.0.1:{}{}".format(self.controller_port, path)
        headers = {"Authorization": "Bearer " + self.secret}
        data = None
        method = "GET"
        if selected is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps({"name": selected}).encode("utf-8")
            method = "PUT"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        with direct_opener().open(request, timeout=5) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    def select(self, name):
        path = "/proxies/{}".format(urllib.parse.quote("REGISTER_EGRESS", safe=""))
        self.controller(path, selected=name)

    def start(self):
        core = mihomo_binary()
        self.workdir = tempfile.TemporaryDirectory(prefix="register_loadtest_")
        home = Path(self.workdir.name)
        print("[*] 正在下载订阅并载入包内 Mihomo...", flush=True)
        provider_path = home / "provider.yaml"
        fetch_subscription(self.cfg["subscription_url"], provider_path,
                           self.cfg["timeout_seconds"])
        profile = prepare_subscription(provider_path)
        self.proxy_port = local_port()
        self.controller_port = local_port()
        while self.controller_port == self.proxy_port:
            self.controller_port = local_port()
        config = mihomo_config(profile, self.proxy_port,
                               self.controller_port, self.secret, self.cfg)
        config_path = home / "config.yaml"
        config_path.write_text(config, encoding="utf-8")
        self.log = (home / "mihomo.log").open("wb")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(core), "-d", str(home), "-f", str(config_path)],
            cwd=str(home), stdin=subprocess.DEVNULL, stdout=self.log,
            stderr=subprocess.STDOUT, creationflags=flags,
        )
        self.opener = proxy_opener(self.proxy_port)
        self._wait_for_health()

    def _wait_for_health(self):
        deadline = time.monotonic() + 120
        last = "控制器未就绪"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Mihomo 启动失败，退出码 {}".format(
                    self.process.returncode))
            try:
                provider = self.controller("/providers/proxies/subscription")
                proxies = provider.get("proxies", [])
                if proxies:
                    tested = [item for item in proxies if item.get("history")]
                    if len(tested) == len(proxies):
                        self.nodes = [item["name"] for item in proxies
                                      if item.get("alive")]
                        if not self.nodes:
                            raise RuntimeError(
                                "订阅节点健康检查全部失败；没有向目标发送注册请求")
                        print("[+] 订阅载入 {} 个节点，健康节点 {} 个".format(
                            len(proxies), len(self.nodes)), flush=True)
                        return
                    last = "节点健康检查 {}/{}".format(len(tested), len(proxies))
                else:
                    last = "订阅 provider 未载入任何节点"
            except RuntimeError:
                raise
            except (OSError, urllib.error.URLError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(0.25)
        raise RuntimeError("代理池启动超时: {}".format(last))

    def stop(self):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            self.process = None
            if self.log is not None:
                self.log.close()
                self.log = None
            if self.workdir is not None:
                self.workdir.cleanup()
                self.workdir = None

    def exit_ip(self, node):
        self.select(node)
        return query_exit_ip(self.opener, self.cfg)


class DirectPool:
    """Use the machine's actual egress without inheriting OS proxy settings."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.opener = direct_opener()
        self.nodes = ["DIRECT"]
        self.process = None

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        pass

    def select(self, _node):
        pass

    def exit_ip(self, _node):
        return query_exit_ip(self.opener, self.cfg)


def query_exit_ip(opener, cfg):
    request = urllib.request.Request(
        cfg["exit_ip_url"], headers={"User-Agent": USER_AGENT,
                                      "Connection": "close"})
    with opener.open(request, timeout=cfg["timeout_seconds"]) as response:
        data = json.load(response)
    return str(ipaddress.ip_address(data["ip"]))


def identify_exits(pool):
    """Associate nodes with verified public exit IPs; share quotas by IP."""
    result = []
    for i, name in enumerate(pool.nodes, 1):
        try:
            exit_ip = pool.exit_ip(name)
            result.append(Node(name, exit_ip))
        except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
            print("[!] 节点 {}/{} 出口探测失败: {}".format(
                i, len(pool.nodes), type(exc).__name__), flush=True)
        if i % 10 == 0 or i == len(pool.nodes):
            print("[*] 出口识别 {}/{}，可用 {}".format(
                i, len(pool.nodes), len(result)), flush=True)
    if not result:
        raise RuntimeError("没有可验证真实出口 IP 的节点")
    print("[+] {} 个节点，{} 个独立出口 IP".format(
        len(result), len({node.exit_ip for node in result})), flush=True)
    return result


def retry_after_seconds(raw):
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def credentials(domain):
    email = "{}@{}".format(secrets.token_hex(7), domain)
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(16))
        if (any(char.islower() for char in password)
                and any(char.isupper() for char in password)
                and any(char.isdigit() for char in password)):
            return email, password


def post_registration(pool, url, email, password, invitation, timeout):
    payload = {"email": email, "password": password}
    if invitation:
        payload["invitation_code"] = invitation
    origin = urllib.parse.urlsplit(url)
    site = "{}://{}".format(origin.scheme, origin.netloc)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Origin": site, "Referer": site + "/register",
                 "Connection": "close", "User-Agent": USER_AGENT},
    )
    started = time.monotonic()
    try:
        try:
            response = pool.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            retry = response.headers.get("Retry-After", "")
            raw = response.read(1024 * 1024)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        elapsed = round((time.monotonic() - started) * 1000)
        return None, {}, "", elapsed, "proxy transport: {}".format(
            type(exc).__name__)
    elapsed = round((time.monotonic() - started) * 1000)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        body = {}
    return status, body, retry, elapsed, ""


def classify(status, body, transport_error):
    if transport_error:
        return "error", transport_error
    if isinstance(body, dict):
        detail = str(body.get("reason") or body.get("message") or "")
    else:
        detail = ""
    if status == 200 and isinstance(body, dict) and body.get("code") == 0:
        return "ok", "created"
    if status == 429:
        return "throttled", detail or "HTTP 429"
    if status == 404:
        return "notfound", detail or "HTTP 404"
    if status is not None and 500 <= status <= 599:
        return "error", detail or "HTTP {}".format(status)
    return "rejected", detail or "HTTP {}".format(status)


def run_live(cfg, limit, check_pool=False):
    base = cfg["target_url"].rstrip("/")
    url = base + REGISTER_PATH
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = ROOT / "results"
    summary = {key: 0 for key in (
        "ok", "throttled", "rejected", "notfound", "error")}
    sent = 0
    started = time.monotonic()
    reason_stopped = ""

    with (MihomoPool(cfg) if cfg.get("enable_dynamic_ip_pool", True)
          else DirectPool(cfg)) as pool:
        nodes = identify_exits(pool)
        if check_pool:
            return 0
        ledger = CooldownScheduler(nodes, cfg["quota_per_ip"],
                                   cfg["window_seconds"])
        result_dir.mkdir(exist_ok=True)
        csv_path = result_dir / "register_result_{}.csv".format(stamp)
        account_path = result_dir / "register_accounts_{}.tsv".format(stamp)
        print("[*] 目标 {} | 上限 {} | 每出口 {} 次/首次调用起 {} 秒".format(
            url, limit or "无限制", cfg["quota_per_ip"],
            cfg["window_seconds"]), flush=True)
        print("[*] 明细 {}".format(csv_path), flush=True)
        try:
            with csv_path.open("w", newline="", encoding="utf-8-sig") as output, \
                    account_path.open("w", encoding="utf-8") as accounts:
                writer = csv.DictWriter(output, fieldnames=RESULT_FIELDS)
                writer.writeheader()
                output.flush()
                while not limit or sent < limit:
                    if pool.process is not None and pool.process.poll() is not None:
                        reason_stopped = "Mihomo 意外退出"
                        break
                    node, wait = ledger.reserve()
                    if node is None:
                        time.sleep(min(wait, 1.0))
                        continue
                    pool.select(node.name)
                    email, password = credentials(cfg["email_domain"])
                    status, body, retry, latency, transport_error = post_registration(
                        pool, url, email, password, cfg["invitation_code"],
                        cfg["timeout_seconds"],
                    )
                    sent += 1
                    verdict, detail = classify(status, body, transport_error)
                    summary[verdict] += 1
                    if verdict == "throttled":
                        window_end = (ledger.window_start[node.exit_ip]
                                      + cfg["window_seconds"])
                        ledger.penalize(node.exit_ip, max(
                            retry_after_seconds(retry),
                            window_end - ledger.clock()))
                    writer.writerow({
                        "number": sent,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "node": node.name,
                        "exit_ip": node.exit_ip,
                        "status": status or "",
                        "verdict": verdict,
                        "latency_ms": latency,
                        "email": email,
                        "detail": detail,
                    })
                    output.flush()
                    if verdict == "ok":
                        accounts.write("{}\t{}\n".format(email, password))
                        accounts.flush()
                    if sent <= 10 or sent % 100 == 0 or verdict not in ("ok",):
                        print("[{:>6}] {:<10} HTTP {}  IP {}  {}".format(
                            sent, verdict, status or "-", node.exit_ip, detail),
                            flush=True)

                    challenge = any(marker in detail.upper() for marker in (
                        "TURNSTILE", "CAPTCHA", "VERIFICATION_FAILED"))
                    if (cfg["stop_on_verification"] and challenge):
                        reason_stopped = "目标要求交互验证"
                        break
                    if verdict == "notfound":
                        reason_stopped = "注册路径不存在"
                        break
        except KeyboardInterrupt:
            reason_stopped = "手动停止"
            print("\n[*] 收到 Ctrl+C，正在保存结果...", flush=True)

    elapsed = time.monotonic() - started
    rate = summary["ok"] / sent * 100 if sent else 0.0
    print("[+] 完成 {} 次；成功 {}，限流 {}，拒绝 {}，404 {}，错误 {}".format(
        sent, summary["ok"], summary["throttled"], summary["rejected"],
        summary["notfound"], summary["error"]), flush=True)
    print("[+] 成功率 {:.2f}% | 耗时 {:.1f}s".format(rate, elapsed), flush=True)
    print("[+] 账号 {}".format(account_path), flush=True)
    if reason_stopped:
        print("[!] 提前停止：{}".format(reason_stopped), flush=True)
        return 2
    return 0


def simulate(count):
    """Exercise the scheduler without network, a proxy, or real accounts."""
    now = [0.0]
    nodes = [Node("n1", "198.51.100.1"), Node("n2", "198.51.100.1"),
             Node("n3", "198.51.100.2"), Node("n4", "198.51.100.3")]
    scheduler = CooldownScheduler(nodes, quota=5, window=60,
                                  clock=lambda: now[0])
    checked = {}
    for _ in range(count):
        while True:
            node, delay = scheduler.reserve()
            if node is not None:
                break
            now[0] += delay
        anchor, used = checked.get(node.exit_ip, (now[0], 0))
        if now[0] >= anchor + 60:
            anchor, used = now[0], 0
        used += 1
        if used > 5:
            raise AssertionError("IP cooldown violated")
        checked[node.exit_ip] = (anchor, used)
    print("[+] 本地模拟 {} 次：全部通过；3 个独立出口，耗时 {:.0f}s（虚拟时间）".format(
        count, now[0]), flush=True)
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--limit", type=int, help="本次上限；0 为无限制")
    parser.add_argument("--check-pool", action="store_true",
                        help="只检测订阅、代理节点和出口，不发送注册请求")
    parser.add_argument("--simulate", type=int, metavar="COUNT",
                        help="无网络本地模拟 COUNT 次，验证 IP 冷却")
    args = parser.parse_args(argv)
    if args.simulate is not None:
        if args.simulate < 1:
            parser.error("--simulate 必须大于 0")
        return simulate(args.simulate)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit 不能为负数")
    try:
        cfg = read_config(args.config)
        limit = cfg["limit"] if args.limit is None else args.limit
        return run_live(cfg, limit, check_pool=args.check_pool)
    except KeyboardInterrupt:
        print("\n[*] 已停止", flush=True)
        return 130
    except Exception as exc:
        print("[!] 启动失败: {}: {}".format(type(exc).__name__, exc),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

