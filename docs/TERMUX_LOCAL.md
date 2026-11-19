# Y700 / Termux 本地签名部署

面向原生 Termux ARM64 环境（Android 16、Termux F-Droid 0.118.3、Python 3.14、Go 1.27、Zig 0.17 和 uv 0.13）。此前 HTTP 签名版本已由用户在 Y700 编译并调用成功；新版 IPC 仍需在设备上重新编译验证。

Python 通过子进程的 stdin/stdout 管道请求本地签名，不监听签名端口，不支持远程签名节点。本地签名器缺失或启动失败时停止网关启动。签名器仍需连接 Google 获取挑战，模型请求也仍发送给 Google。

## 已部署用户更新

先在运行网关的终端按 Ctrl+C，等待退出，再执行：

```bash
cd ~/aistudio-api-y700
git pull --ff-only
bash scripts/setup-env.sh
GOMAXPROCS=2 GOFLAGS="-p=2" bash waa-signer/build.sh
```

必须重新编译签名器；旧 HTTP 二进制无法处理新 IPC 协议。请保留已有 `.env`、`data/config.yaml` 和账号数据，**不要重新执行下方首次初始化**。

旧 `.env` 中的 `AISTUDIO_PROOF_SERVER_URL`、旧运行配置的 `waa` 节点列表已不再使用，可自行删除；保留它们不会启用远程节点。管理密码、API Key、账号和网关端口继续使用原有配置。新版不再占用 8081 端口，也不再提供 `8081/health`。

更新后可独立检查签名器，无需 Cookie，也不会发起模型请求：

```bash
./waa-signer/bin/waa-signer -check
```

预期输出包含 `"ok":true` 和 `"mode":"ipc"`。日常启动仍可使用原命令并指定原来的网关端口：

```bash
uv run --no-sync python main.py server --port 8080
```

这里 8080 只是示例，请替换为自己实际使用的端口。也可以运行 `bash start.sh`，默认读取现有 `.env` / 环境变量中的 `AISTUDIO_PORT`。

## 1. 首次准备依赖和代码

在 Termux 自身的终端执行，避免放在 `/sdcard` 中编译或运行：

```bash
pkg update
pkg install -y git python uv golang zig curl ca-certificates resolv-conf
cd ~
umask 077
git clone https://github.com/Kiro-Durandal/aistudio-api.git aistudio-api-y700
cd ~/aistudio-api-y700
bash scripts/setup-env.sh
```

`setup-env.sh` 生成未跟踪的 `uv.toml`，指定 Termux 系统 Python、禁止下载其他 Python，并添加 TUR 社区预编译包源，随后安装 Python 依赖。该分支不自动切换到额外的下载镜像，网络不可用时请配置自己的代理后重试。

## 2. 编译本地签名器

```bash
go version
zig version
GOMAXPROCS=2 GOFLAGS="-p=2" bash waa-signer/build.sh
```

Go 必须为 1.27 或以上。成功后生成 `waa-signer/bin/waa-signer`。构建脚本从 Mag1cFall/firefox 的 GitHub Release 获取预编译 SpiderMonkey 静态库，并按仓库的 `spidermonkey.sha256` 校验下载包；此前未校验的缓存会重新下载。校验只能验证下载内容一致，仍需信任静态库发布者，不能替代对其构建链和二进制的审计。

编译失败时先排查，不使用公共签名节点作为回退。

## 3. 首次初始化密码和运行配置

下面代码仅供首次部署，遇到已有 `.env` 或 `data/config.yaml` 会退出，以免覆盖凭据。生成的密码和 API Key 只显示在自己的终端，不要贴进聊天或提交到 Git。

```bash
uv run --no-sync python - <<'PY'
import secrets
from pathlib import Path

import yaml

env_path = Path(".env")
runtime_path = Path("data/config.yaml")
if env_path.exists() or runtime_path.exists():
    raise SystemExit("已有本地配置，请保留并检查现有密码与配置。")
admin = secrets.token_urlsafe(32)
api_key = secrets.token_urlsafe(32)
config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
config["api_keys"] = [api_key]
config["logging"]["dump_requests"] = False
config["logging"]["dump_to_file"] = False
runtime_path.parent.mkdir(mode=0o700, exist_ok=True)
runtime_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
runtime_path.chmod(0o600)
env_path.write_text(
    f"AISTUDIO_WEB_PASSWORD={admin}\n"
    "AISTUDIO_CONFIG_FILE=data/config.yaml\n"
    "AISTUDIO_HOST=127.0.0.1\n"
    "DEBUG=0\nAISTUDIO_DUMP_REQUESTS=0\nAISTUDIO_DUMP_FILE=0\n",
    encoding="utf-8",
)
env_path.chmod(0o600)
print("管理密码：", admin)
print("客户端 API Key：", api_key)
PY
```

密码保存在 `.env`，API Key 保存在 `data/config.yaml`；两个路径均被 `.gitignore` 排除。

## 4. 启动与检查

```bash
uv run --no-sync python main.py server --port 8080
```

默认只监听 `127.0.0.1`，也可通过 `.env` 的 `AISTUDIO_HOST` 配置；统一入口 `main.py server` 只接受 `--port`。保持终端运行，在 Termux 新会话检查网关：

```bash
curl --noproxy '*' -f http://127.0.0.1:8080/health
```

健康检查不证明 Google 账号或挑战已验证成功。管理面板的系统设置可查看本地 IPC 引擎状态。

浏览器访问 `http://127.0.0.1:8080`，输入管理密码再导入 Cookie。客户端选择 **Google Gemini** 协议，Base URL 使用同一地址，API Key 使用自己的客户端密钥。本机回环地址不会向局域网发布服务，同设备应用仍可能访问网关，需保留管理密码和 API Key。

## 5. 网络与后台运行

签名器与 Python 网关都必须能访问 Google。使用 VPN 时确认 Termux 包含在代理范围；显式 HTTP/SOCKS 代理可在 `.env` 的 `AISTUDIO_PROXY` 中设置。Python 到签名器始终走子进程管道。

### 静态签名器的 DNS

Go 签名器编译为 Linux 静态程序，不能自动使用 Android 系统 DNS。本分支保留 `$PREFIX/etc/resolv.conf` 支持，不再依赖 Linux `/etc/resolv.conf`，也不会自行添加公共 DNS。

`resolv-conf` 是 Termux 官方包，首次安装默认包含 Google DNS（8.8.8.8 和 8.8.4.4）；已有配置请按自己的网络需求保留。也可设置 `AISTUDIO_DNS_SERVER=实际DNS服务器IP`，支持 `IP:端口` 或 `[IPv6]:端口`。DNS 查询不包含 Cookie 或签名请求内容。

不带账号凭据的 DNS 检查：

```bash
uv run --no-sync --env-file .env ./waa-signer/bin/waa-signer -check-dns
```

锁屏后需要继续运行时，可使用 `termux-wake-lock`，并在 Android 设置中取消 Termux 电池优化。停止使用后执行 `termux-wake-unlock`。
