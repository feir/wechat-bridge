# WeChat Bridge

在微信中使用 Claude Code — 通过 iLink Bot API 将微信消息桥接到 Claude Code CLI，支持文本、语音（ASR 转文字）、图片（CDN 下载 + AES 解密 + 视觉理解）。

## 它能做什么

```
"帮我分析这段代码的问题"                → Claude Code 读代码、给反馈
[发送一张截图]                         → CDN 下载 → AES 解密 → Claude 视觉分析
[发送一条语音]                         → ASR 转文字 → Claude 理解并回复
"/status"                             → 查看 context 用量、token 费用
"/compact"                            → 压缩上下文，延长会话寿命
```

## 核心特性

### 多模态消息
- **文本** — 直接发送，Claude Code 处理并回复
- **语音** — 自动提取微信 ASR 转写文字，无需额外模型
- **图片** — CDN 下载 + AES-128-ECB 解密 + Claude 视觉理解

### Bridge 命令

| 命令 | 说明 |
|------|------|
| `/new` | 重置会话（清除上下文，开始新对话） |
| `/stop` | 停止当前正在运行的任务 |
| `/compact` | 压缩上下文（释放 token 空间） |
| `/status` | 查看会话状态（context 用量 / 费用 / 模型） |
| `/help` | 显示帮助 |

### 上下文管理
- 跨消息维持完整上下文，bridge 重启自动恢复会话
- Context 用量监控（70% / 85% 阈值），自动在回复末尾提示
- `/compact` 压缩上下文，`/new` 重置会话

### 多用户支持
- 每用户独立会话与任务队列
- 主用户 / 访客用户权限分离，访客独立费用上限
- 多用户工作区隔离（可选）

### CLI 工具

`wechat-cli` 提供消息发送和用户管理命令，可用于通知推送等场景：

```bash
wechat-cli send-message --user-id <id> --text "部署完成"
wechat-cli send-message --broadcast --text "系统维护通知"
wechat-cli list-users
```

## 快速开始

### 1. 获取 iLink Bot 凭证

通过微信 OpenClaw 平台创建 iLink Bot，获取 bot_token 和 base_url。

### 2. 安装

```bash
pipx install wechat-bridge        # 推荐（隔离环境）

# 或从源码
git clone https://github.com/feir/wechat-bridge.git
cd wechat-bridge && pip install -e '.[dev]'
```

### 升级

```bash
# PyPI 安装
pipx upgrade wechat-bridge

# 源码安装
cd wechat-bridge && git pull
pip install -e '.[dev]'           # 仅依赖变更时需要
```

### 3. 认证

```bash
python -m wechat_bridge.ilink_auth
# 交互式输入 bot_token 和 base_url
# → 凭证写入 ~/.config/wechat-bridge/credentials.json
```

### 4. 配置

创建 `.env` 文件（参考 `.env.example`）：

```bash
WECHAT_ALLOWED_USERS=user_id_1,user_id_2
CLAUDE_MODEL=sonnet
CLAUDE_TIMEOUT=300
WECHAT_MAX_CONCURRENT=3
```

### 5. 运行

```bash
wechat-bridge
```

## 部署

```bash
# systemd（Linux）
cp wechat-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wechat-bridge
```

## 开发

```bash
pip install -e '.[dev]'
pytest tests/ -v
```

## License

MIT
