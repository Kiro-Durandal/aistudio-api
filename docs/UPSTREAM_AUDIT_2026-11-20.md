# 2026-11-20 上游增量审计与同步

审计范围：DeconstructedCube/aistudio-api 的 `dc8e951`、`3353d0d`、`c34a263`，以及它们与本分支仅限本地签名、回环监听和 Termux DNS 修复的合并结果。本次不是全仓库和全部第三方二进制的独立安全认证。

## 结论

修复下述问题后，同步至上游 `c34a2636e0a7a602972e8bfba6cff9a11bbbf50b`。签名改为 Python 与本地 Go 子进程的 stdio NDJSON IPC，不再开放 8081 签名端口，不保留公共签名节点配置或回退。网关默认仍监听 `127.0.0.1`，本地签名器缺失或启动失败时拒绝启动。未添加 RikkaHub 专用协议兼容处理。

## 发现与处理

| 问题 | 影响 | 处理与验证 |
|---|---|---|
| 旧 IPC 读取任务和 stop 清理可作用于新进程的共享字段 | 重启时新请求被错误失败或取消 | 每代读取任务绑定自己的 Future 集合；启动、停止串行；相关测试在上游代码失败、修复后通过 |
| 写管道 drain 未纳入请求超时 | 子进程不读 stdin 时请求可无限等待 | 超时覆盖排队、写入及等待响应；加入阻塞写入回归测试 |
| Go 收到 stdin EOF 后先等请求，直到函数退出才取消上下文 | 正在进行的签名可拖延退出 | 在等租约前取消签名；上游 EOF 回归测试失败、修复后通过 |
| 终止信号只取消上下文，无法可靠打断静态二进制的阻塞 stdin 读取 | 空闲子进程收到 SIGTERM 后仍驻留 | 输入读取与请求分发分离，分发可取消；真实 Linux 静态二进制验证通过 |
| 安装脚本重新引入第三方镜像工具执行，静态库仅检查压缩包完整性 | 增加下载供应链信任范围 | 移除自动镜像回退；固定 SpiderMonkey Release 的 SHA-256，并重新验证旧缓存；保留用户自选代理和源 |
| Termux 配置未保留本分支的 Python 下载限制与 DNS 设置 | 可能选用不兼容 Python 或无法解析 Google 域名 | 保留系统 Python 限制、resolv-conf 安装和 netresolve 集成；精简 uv 索引配置 |
| 启动脚本始终传入默认 8080，盖过 .env 配置 | 自定义网关端口失效 | Unix 脚本只在显式指定时传入 --port |
| 单元测试漏掉配额查询模拟 | 测试会意外尝试连接 Google | 补齐模拟；测试套件统一禁止 socket 连接和 DNS 查询 |

此外，避免将代理 URL 和非法 IPC 原始响应写入日志；修正 Windows 安装脚本的具名参数传递。

## 验证

- Python：431 项单元测试全部通过，测试期间禁止 Python socket 网络访问；Ruff 检查、格式检查及 Pyright 均通过。
- Go：`bash waa-signer/build.sh --test` 与 `-race` 检查通过，覆盖 IPC、Worker 复用、Cookie 代际和 Termux DNS。
- Linux x86_64：实际构建静态签名器并通过自检；32 个并发 IPC health 请求、无 Cookie 请求校验、SIGTERM、重启与停止检查通过。
- 前端：ESLint、TypeScript 检查和生产构建通过，生成文件与本次上游构建产物一致。
- Shell：安装、启动与构建脚本的 `bash -n` 检查通过。

未在本工作环境运行 Android ARM64 或 Windows；新版需在 Y700 按 [更新步骤](./TERMUX_LOCAL.md) 重编并验证。未使用用户 Cookie 发起真实 Google 模型请求。

## 仍需信任的部分

SpiderMonkey 使用 Mag1cFall/firefox 发布的预编译静态库。SHA-256 只能确认下载内容与固定发布包一致，不能证明其没有恶意代码；TUR 的 Android Python wheel 同样需要信任其维护者。签名器仍需向 Google 获取挑战，模型请求仍发送给 Google。本地 IPC 并不能保护已经被入侵的 Termux 环境或设备。
