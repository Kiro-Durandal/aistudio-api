# Y700 / Termux 本地签名部署

本说明面向原生 Termux ARM64 环境。参考环境为 Android 16、Termux F-Droid 0.118.3、Python 3.14、Go 1.27、Zig 0.17 和 uv 0.13。它们是参考版本，本文尚未在实际 Y700 上完成编译或 Google 账号实测。

本分支只允许将签名请求发送到 `http://127.0.0.1:8081`，不继承签名客户端的环境代理，也不跟随 HTTP 跳转。本地签名不可用时停止启动。签名器仍需连接 Google 获取挑战，模型请求也仍发送给 Google。

## 1. 准备依赖和代码

从 F-Droid 或 Termux 官方 GitHub Releases 安装 Termux。下面命令在 Termux 自身的终端执行：

```bash
pkg update
pkg install -y git python uv golang zig curl ca-certificates
cd ~
umask 077
git clone https://github.com/Kiro-Durandal/aistudio-api.git aistudio-api-y700
cd ~/aistudio-api-y700
bash scripts/setup-env.sh
uv sync --python "$PREFIX/bin/python" --no-dev
```

`setup-env.sh` 为当前项目生成未跟踪的 `uv.toml`：使用 Termux 已安装的 Python、禁止自动下载其他 Python，并添加项目原有的 TUR 社区预编译包源。不要在 `/sdcard` 中编译或运行项目。

## 2. 编译本地签名器

```bash
go version
zig version
GOMAXPROCS=2 GOFLAGS="-p=2" bash waa-signer/build.sh
```

Go 必须为 1.27 或以上。成功后生成 `waa-signer/bin/waa-signer`。构建脚本从 Mag1cFall/firefox 的 GitHub Release 获取预编译 SpiderMonkey 静态库，不回退到第三方下载镜像；这些库仍需要信任其发布者，当前脚本未提供哈希校验。

任意一步失败都先排查该步骤，不要通过公共签名节点绕过编译失败。

## 3. 初始化密码和运行配置

下面代码仅供首次部署，遇到已有 `.env` 或 `data/config.yaml` 会退出，以免覆盖原有凭据。生成的密码和 API Key 只显示在自己的终端，不要贴进聊天或提交到 Git。

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
config["waa"]["active_server"] = "local_spidermonkey"
config["logging"]["dump_requests"] = False
config["logging"]["dump_to_file"] = False
runtime_path.parent.mkdir(mode=0o700, exist_ok=True)
runtime_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
runtime_path.chmod(0o600)
env_path.write_text(
    f"AISTUDIO_WEB_PASSWORD={admin}\n"
    "AISTUDIO_CONFIG_FILE=data/config.yaml\n"
    "AISTUDIO_HOST=127.0.0.1\n"
    "AISTUDIO_PROOF_SERVER_URL=http://127.0.0.1:8081\n"
    "DEBUG=0\nAISTUDIO_DUMP_REQUESTS=0\nAISTUDIO_DUMP_FILE=0\n",
    encoding="utf-8",
)
env_path.chmod(0o600)
print("管理密码：", admin)
print("客户端 API Key：", api_key)
PY
```

密码保存在 `.env`，API Key 保存在 `data/config.yaml`；这两个路径均被 `.gitignore` 排除，仓库跟踪的 `config.yaml` 不会写入生成的密钥。

## 4. 启动与检查

```bash
uv run --no-sync python main.py server --host 127.0.0.1 --port 8080
```

保持这个终端运行，在 Termux 新建另一个会话检查：

```bash
curl --noproxy '*' -f http://127.0.0.1:8081/health
curl --noproxy '*' -f http://127.0.0.1:8080/health
```

第一个响应应包含 `"ok": true` 和 `"engine": "spidermonkey-152"`。它只证明本地签名进程已启动，不证明账号或 Google 挑战已验证成功。

浏览器访问 `http://127.0.0.1:8080`，输入管理密码，确认选中本地 SpiderMonkey 节点后再导入 Cookie。客户端选择 **Google Gemini** 协议，Base URL 为 `http://127.0.0.1:8080`，API Key 使用刚生成的客户端密钥。

本机回环地址不会向局域网发布端口；同设备其他应用仍可能访问这些端口，因此管理密码和 API Key 仍应保留。

## 5. 网络与后台运行

签名器和 Python 网关都必须能访问 Google。如果使用 VPN，请确认 Termux 流量包含在代理范围；如果使用显式 HTTP/SOCKS 代理，在 `.env` 的 `AISTUDIO_PROXY` 中填写实际可用的地址。该设置影响到 Google 的出口，Python 到本地签名器的请求始终不走代理。

可通过 Termux 通知中的 Acquire Wakelock 及 Android 的电池设置保持后台运行。初次排错先保持前台，停止服务用 Ctrl+C；本地签名器会随网关退出。

如果提示“本地签名服务不可用”，可单独执行下面命令查看签名器输出，再在另一个会话启动网关：

```bash
./waa-signer/bin/waa-signer -listen 127.0.0.1:8081
```

调试完成后用 Ctrl+C 关闭这个手动进程。不要把 `.env`、`data/`、Cookie、密钥或含完整请求的转储发到 Issues。
