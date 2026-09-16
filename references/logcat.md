# android-use Logcat 参考

## 持续采集

在动作前启动一次，后续直接查看返回的文件：

```bash
android-use logcat start --package com.example.app
# ~/.android-use/logs/au-com.example.app-<ts>.log
android-use logcat status
rg 'eventId:|AndroidRuntime' <返回的日志文件>
android-use logcat stop
```

- 每台设备只维护一个采集器；重复 `start` 返回现有健康采集路径。
- 默认使用 `adb logcat -T 1`，不清空 ring buffer。只有显式 `start --clear` 才执行
  破坏性的 `logcat -c`。
- `--package` 只命名文件并记录意图；文件保留全设备日志，覆盖 App 重启、多进程、
  ActivityManager、AndroidRuntime 和输入法。
- `status` 报告 supervisor/ADB 子进程、文件字节数、更新时间、最后设备时间戳和
  flow-control 警告。没有 `read` 子命令。
- 高层命令执行期间会写入脱敏 start/end marker 和本地 timeline，便于缩小 grep 窗口。

不只看当前 App 时省略 `--package`：

```bash
android-use logcat start
# ~/.android-use/logs/au-all-<ts>.log
```

两种模式都采集全设备日志。

默认产物和后台状态位于 `~/.android-use/` 的私有目录，CLI 创建文件时使用私有权限。
日志、截图、UI XML 和抓包仍可能包含其他 App 的个人数据、Cookie、Authorization 和
响应正文；显式输出路径由调用者管理。不要未经检查上传产物或把它们提交进仓库。

## 证据边界

厂商 logd 可能在写入 ring buffer 前限流。出现 `LOG_FLOWCTRL`、`OVER PROC QUOTA`、
`DROPPED`、`chatty` 或采集器不健康时，目标行缺失不能证明事件未触发。结合 UI 状态、
持久化值、网络请求或独立结果文件交叉确认。

```bash
adb shell getprop persist.logd.flowctrl.on
adb shell getprop persist.logd.flowctrl.quota.rows
adb shell getprop persist.logd.flowctrl.quota.size
```

这些属性不是 Android 通用合约，不要在普通设备上修改全局限流配置。
