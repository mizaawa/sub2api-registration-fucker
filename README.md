# 杂鱼 API

正在为 AI 项目寻找 API 接入服务？欢迎了解 **[杂鱼 API](https://zayuapi.com)**。访问官网查看服务信息与接入文档，为你的项目选择合适的接入方案，把更多时间留给产品与代码。具体服务内容以官网公布的信息为准。

---

# sub2api-registration-fucker

**本地离线响应模拟与调度测试。** 当前公开版本使用合成响应验证固定总速率、限流等待和验证失败停机，并生成可检查的 CSV 报告。

本版本不连接真实站点、不创建真实账号、不读取订阅、不启动代理，也不包含在线批量注册功能。文中的 HTTP 状态均为本地生成的测试数据，模拟通过不代表真实服务注册成功。

## 功能

- **零第三方依赖**：Python 3.10+ 即可运行。
- **离线模拟**：无须 API Key、订阅链接或目标地址。
- **固定总速率**：所有模拟事件遵守一个统一的时间间隔。
- **429 场景**：第三个事件模拟限流，整个模拟队列等待 30 秒虚拟时间。
- **验证场景**：第三个事件模拟验证要求，立即停止后续事件。
- **CSV 报告**：输出序号、虚拟时间、状态和结果，明确标记为合成数据。
- **Windows 双击启动**：启动器会保留无参数运行的结果窗口。

## 快速开始

### Windows

安装 Python 3.10 或更高版本，并勾选添加到 PATH。克隆仓库或下载 ZIP 并解压，然后进入项目目录。

双击 `run-local.cmd` 即可运行默认的 10,000 次成功场景模拟。这里的次数是内存中的模拟事件数，不是实际网络请求数。

PowerShell 中可以运行：

```powershell
.\run-local.cmd
.\run-local.cmd --count 100 --rate 5 --scenario rate-limit
```

### macOS / Linux

```bash
python3 offline_demo.py
python3 offline_demo.py --count 100 --rate 5 --scenario rate-limit
```

工具不需要安装 Mihomo、Clash、PyYAML 或其他依赖，也不需要填写 `config.json`。

## 命令行参数

```text
python offline_demo.py [--count N] [--rate N] [--scenario NAME] [--output PATH]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--count` | `10000` | 最多生成的事件数，范围为 1 到 1,000,000。 |
| `--rate` | `5` | 每秒虚拟时间的最大事件数，范围为 1 到 1,000。 |
| `--scenario` | `success` | 场景：`success`、`rate-limit`、`verification`。 |
| `--output` | 不写文件 | 可选 CSV 路径，父目录自动创建；已有文件不会被覆盖。 |
| `--help` | 无 | 查看帮助。 |

### 成功场景

```powershell
python offline_demo.py --count 10000 --rate 5
```

所有合成响应均为 200。程序输出 JSON 摘要，包括请求的事件数、实际生成的事件数、各类结果计数和虚拟耗时。

### 限流场景

```powershell
python offline_demo.py --count 10 --rate 5 --scenario rate-limit
```

第三个事件为合成 429，第四个事件最早在 30 秒虚拟时间后执行。等待作用于整个队列，不使用节点切换或 IP 轮换。

### 验证失败场景

```powershell
python offline_demo.py --count 10 --scenario verification
```

第三个事件为合成 403，结果标记为 `verification_required`，后续事件不再生成。该场景的退出码为 `2`，表示按预期提前停止。

> 场景触发点固定在第三个事件；`--count` 小于 3 时不会触发 429 或验证要求。

### 导出报告

```powershell
python offline_demo.py --count 100 --output results/demo.csv
```

使用新的文件名保存下一份报告，避免覆盖已有结果。

| CSV 字段 | 含义 |
| --- | --- |
| `number` | 事件序号，从 1 开始。 |
| `virtual_seconds` | 相对起点的虚拟时间，单位为秒。 |
| `http_status` | 合成的 HTTP 状态码，不来自真实服务器。 |
| `outcome` | `ok`、`throttled` 或 `verification_required`。 |
| `synthetic` | 固定为 `True`，表示离线合成数据。 |

模拟不调用真实休眠。摘要中的虚拟耗时通常远大于程序实际执行时间，这是预期行为。

## 测试

```powershell
python -B -m unittest discover -s tests -v
```

测试覆盖总速率间隔、全队列限流等待、验证停机、参数边界、CSV 输出、不覆盖已有报告，以及阻断网络连接和子进程后的离线执行。

退出码：正常完成为 `0`；报告写入失败为 `1`；验证场景提前停止或命令行参数错误为 `2`。

## 目录结构

```text
.
|-- offline_demo.py          # 标准库实现的离线模拟入口
|-- run-local.cmd            # Windows 启动器
|-- tests/
|   `-- test_offline_demo.py # 自动化测试
|-- README.md               # 使用说明
|-- PROMOTION.md            # 可单独分享的项目介绍文案
`-- .gitignore              # 本地配置、凭据和产物排除规则
```

## 隐私与发布

公开仓库不需要、也不应包含个人订阅 URL、代理节点凭据、API Key、账号密码或真实运行结果。

`.gitignore` 已排除常见的私人配置、环境变量文件、凭据文件、CSV/TSV 结果、日志、缓存和发行压缩包。提交时仍应检查 `git diff --cached`，只暂存明确需要发布的源码和文档。忽略规则不会自动移除已经跟踪或历史提交中的文件。

如果凭据曾被公开，先在对应服务撤销或轮换，再处理 Git 历史；仅删除当前文件不足以消除泄露。

## 常见问题

**双击提示找不到 Python？** 安装 Python 3.10+ 并添加到 PATH，重新打开终端后运行 `python --version`。Windows 启动器也会尝试 `py -3`。

**在哪里填写订阅或目标地址？** 此公开版本仅做离线测试，没有这些配置项。

**模拟成功能否代表实际接口可用？** 不代表。它只验证本地调度和结果记录逻辑。

**如何停止？** 在终端按 `Ctrl+C`。模拟通常很快结束；Windows 无参数启动结束后等待按键关闭窗口。

**为什么报告写入失败？** 检查路径权限，或换用尚不存在的文件名。工具不会覆盖已有 CSV。

顶部的「杂鱼 API」为推荐链接，不代表与本项目存在官方合作、赞助关系或默认技术集成。
