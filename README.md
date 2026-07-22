# APIBOX
超轻量级本地大模型聊天网站

## 特点

- **专为手机端设计**：直接运行在 Android Termux 中
- **极致轻量**：项目体积小，启动速度快
- **零第三方依赖**：无需执行 `pip install`，仅使用 Python 标准库
- **傻瓜式启动**：只需运行 `python main.py`
- **支持第三方 API**：可接入兼容接口及第三方中转 API
- **本地数据保留**：退出或重启后数据不会自动丢失
- **支持后台保活**：配合 `tmux` 和 `termux-wake-lock` 可长期运行
- **自动重启**：程序异常退出后可自动重新启动
- **自用测试沙盒**：适合 API 调试、接口测试及移动端临时使用

## 快速开始

确保手机已经安装 [Termux](https://github.com/termux/termux-app)，然后执行：

```bash
pkg update -y && pkg install python -y
```

进入项目目录并启动：

```bash
cd apibox
python main.py
```


## 一键启动


```bash
cd apibox && python main.py
```

## Termux 后台保活

安装 `tmux`：

```bash
pkg install tmux -y
```

在项目上级目录执行以下命令，即可创建后台会话并启动 APIBox：

```bash
tmux new-session -s apibox 'cd apibox && termux-wake-lock && while true; do python main.py; code=$?; echo "程序已退出，状态码：$code，2 秒后重新启动..."; sleep 2; done'
```

程序退出或发生异常时，会在 2 秒后自动重新启动。

### 退出到后台

在 `tmux` 会话中依次按下：

```text
Ctrl + B
D
```

### 重新进入 APIBox

```bash
tmux attach -t apibox
```

### 停止后台运行

```bash
tmux kill-session -t apibox
termux-wake-unlock
```

## 第三方中转 API

APIBox 支持接入第三方 API 服务或中转接口。根据服务商提供的信息填写：

- API 地址
- API Key
- 模型名称
- 其他接口参数

请勿将自己的 API Key 提交到 GitHub 或分享给他人。

## 运行要求

- Android
- Termux
- Python 3
- `tmux`，仅后台保活时需要

## 使用场景

- 手机端 API 调试
- 第三方中转 API 测试
- 接口连通性验证
- 临时请求与响应测试
- 无服务器环境下的个人测试沙盒

## 免责声明

本项目主要用于个人学习、开发和接口测试。使用第三方 API 时，请遵守对应服务商的使用条款及当地法律法规。
```

原保活命令中的 `$(date)` 处于外层双引号内，可能在创建 `tmux` 会话时就被展开，导致后续显示的时间不准确。新版命令改为记录退出状态码，也避免了这一问题。另将“0 依赖”明确为“零第三方 Python 依赖”，表述更严谨，因为 Termux 环境仍然需要安装 Python。
