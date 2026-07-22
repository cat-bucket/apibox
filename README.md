# apibox
完美自用测试沙盒 0依赖 数据保留
termux保活 pkg install tmux -y && tmux new -s apibot "cd apibox && termux-wake-lock && while true; do python main.py; echo \"程序于 $(date) 崩溃或退出，2秒后重启...\"; sleep 2; done"
