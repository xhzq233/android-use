# Android ADB 基础操作

android-use 不包装 ADB 已经稳定提供的系统能力。下面命令默认使用唯一设备；
多设备时统一加 `-s SERIAL`，并在状态变更后执行对应检查。
示例使用已安装的 `android-use`；不在 PATH 时用 `~/.local/bin/android-use`。

## 设备

```bash
adb devices -l
adb -s SERIAL get-state
adb -s SERIAL shell getprop ro.product.model
adb -s SERIAL shell wm size
adb -s SERIAL shell wm density
```

对状态为 `device` 的目标执行自动化，不区分 USB、模拟器或网络传输。
`unauthorized` 需要确认调试授权，`offline` 先检查连接并再次枚举。
网络设备先用 `adb pair HOST:PORT`（需要配对时）和 `adb connect HOST:PORT` 建立连接。

## App 和 Activity

```bash
adb -s SERIAL shell pm list packages
adb -s SERIAL shell dumpsys activity activities
adb -s SERIAL shell monkey -p com.example.app 1
adb -s SERIAL shell am force-stop com.example.app
adb -s SERIAL shell input keyevent BACK
adb -s SERIAL shell input keyevent HOME
android-use current-app -s SERIAL
android-use current-app -s SERIAL --format json
```

启停和按键会改变设备状态。`current-app` 返回当前前台 package 和 Activity；
也可用 `dumpsys activity activities` 查看完整系统状态。

安装与卸载：

```bash
adb -s SERIAL install -r /path/to/app.apk
adb -s SERIAL uninstall PACKAGE
adb -s SERIAL shell dumpsys package PACKAGE
```

卸载会删除 App 数据，执行前确认目标包名。

## 点击、滑动和输入

```bash
adb -s SERIAL shell input tap X Y
adb -s SERIAL shell input swipe X1 Y1 X2 Y2 300
adb -s SERIAL shell input text 'hello%sworld'
adb -s SERIAL shell input keyevent ENTER
```

坐标是设备输入像素。stock `input text` 只适合 ASCII 和简单转义，不保证中文、
emoji、剪贴板或复杂输入法行为；这类输入需要明确安装的输入方案或手工完成。

## 截图和文件

```bash
adb -s SERIAL exec-out screencap -p > /tmp/current.png
adb -s SERIAL push /path/to/file /sdcard/Download/
adb -s SERIAL pull /sdcard/Download/file /tmp/
adb -s SERIAL shell run-as PACKAGE ls files
```

需要坐标映射或结构化截图结果时用 `android-use screenshot`。`run-as`
只对可调试 App 生效。

向相册加入测试图片：

```bash
adb -s SERIAL shell mkdir -p /sdcard/Pictures/AutomationTest
adb -s SERIAL push /path/to/image.jpg /sdcard/Pictures/AutomationTest/
adb -s SERIAL shell am broadcast \
  -a android.intent.action.MEDIA_SCANNER_SCAN_FILE \
  -d file:///sdcard/Pictures/AutomationTest/image.jpg
```

任务结束后只清理 `AutomationTest` 隔离目录，不删除用户相册内容。

## DeepLink

```bash
adb -s SERIAL shell am start -W \
  -a android.intent.action.VIEW \
  -c android.intent.category.BROWSABLE \
  -d 'example://screen/<key>'
```

直接用 `adb shell` 时，参数还会经过设备端 shell；复杂 URI 优先用
[DeepLink 参考](deeplink.md) 中的 android-use 命令，它会处理设备端引号。

## 一次性日志

```bash
adb -s SERIAL logcat -d -v threadtime
adb -s SERIAL logcat -d -v threadtime | rg 'AndroidRuntime|eventId:'
```

一次性 `-d` 读取 ring buffer，容易错过触发窗口。需要反复过滤或作为验收证据时，
在动作前启动 [持续 logcat](logcat.md)。

## Proxy 和 ADB reverse

```bash
adb -s SERIAL reverse tcp:8080 tcp:8080
adb -s SERIAL shell settings put global http_proxy 127.0.0.1:8080

adb -s SERIAL shell settings put global http_proxy :0
adb -s SERIAL reverse --remove tcp:8080
```

修改代理前记录原值：

```bash
adb -s SERIAL shell settings get global http_proxy
```

普通抓包优先用 `android-use proxy`，由工具恢复原值并清理 reverse。

## 透传

需要 android-use 的设备选择和超时策略、但仍想执行原始 ADB 时：

```bash
android-use adb -- get-state
android-use adb -- shell input keyevent BACK
android-use adb -- shell sh -c 'device-side pipeline'
```

`--` 后原样传递给 ADB，不经过 host `shell=True`；`adb shell` 的设备端引用规则仍适用。
命令成功只代表 ADB 退出码为零。
