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
[DeepLink 参考](#android-use-deeplink-参考) 中的 android-use 命令，它会处理设备端引号。

## 一次性日志

```bash
adb -s SERIAL logcat -d -v threadtime
adb -s SERIAL logcat -d -v threadtime | rg 'AndroidRuntime|eventId:'
```

一次性 `-d` 读取 ring buffer，容易错过触发窗口。需要反复过滤或作为验收证据时，
在动作前启动 [持续 logcat](#android-use-logcat-参考)。

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

# android-use UI 参考

本文件只描述 android-use 保留的 UI 语义和视觉能力。App、按键、基础输入和文件
操作见 [ADB 基础操作](#android-adb-基础操作)。

## Dump

```bash
android-use dump
android-use dump --stats
android-use dump --raw-out /tmp/page.xml
android-use dump --keep-id-regex '.*:id/(badge|dot)$'
```

- `dump` 复用设备侧系统 UiAutomation 会话，输出清洗后的原子树；首用自动推送仓库
  内置的小型测试 JAR，不安装 APK、不要求手动管理进程，异常时回退系统
  `uiautomator dump --compressed`。
- 不要同时运行原始 `uiautomator dump`、instrumentation 或其它 UiAutomation 工具；
  android-use 只在确认自身会话已退出后才会启动 stock 回退。
- bounds 为设备像素 `(x,y,w,h)`。raw XML 有目标但 clean tree 没有时，可临时用
  `--keep-id-regex`；raw XML 也没有时直接查看截图。
- 目标真机首次启动通常约 1 至 3 秒，热 dump 通常约 0.1 至 0.3 秒；回退 stock 时通常
  为 2 至 4 秒。
- Lynx、自绘画布、颜色、透明度和图片效果通常不在树中，必须看截图。

## Tap 和坐标

```bash
android-use tap 636 2100
android-use tap --normalized 0.5 0.75 --mark /tmp/tap-point.png
android-use tap --from-screenshot /tmp/current.png 540 1200
android-use tap --text "导入照片"
android-use tap --text "目标项目" --scroll-if-needed --container horizontal
android-use tap --text "导入照片" --verify-change
```

- `--normalized` 使用 `[0,1]`；`--from-screenshot` 使用原始图片像素。工具返回最终输入坐标。
- 截图与当前设备宽高比或方向不一致时拒绝映射。
- 文本目标匹配 text、content-desc、resource-id 和 class；子串匹配加 `--contains`，
  重名用 `--match-index`。
- `--verify-change` 只在明确预期 Activity 或树变化时使用。

## Wait

```bash
android-use wait --text "重新生成" --timeout 60s
android-use wait --text-gone "正在加载" --after-seen
android-use wait --activity 'EditActivity' --timeout 20s
android-use wait --log 'request_complete' --timeout 20s
android-use wait --screen-stable 500ms --timeout 10s
```

- 文本等待每轮需要一次 dump；Activity 和持续日志仍是更轻量的信号。
- `--log` 只观察 wait 开始后追加到当前健康 logcat 文件的内容。
- `--screen-stable` 适合树不变的异步画面；自绘文字直接查看截图。
- 超时返回最后状态并自动收集证据；把超时当控制流时加 `--no-evidence`。

## Scroll

```bash
android-use scroll-to --text "目标项目" --container horizontal
android-use tap --text "目标项目" --scroll-if-needed --container horizontal
```

`horizontal/vertical` 由可见滚动容器的宽高启发式判断，不是 App 语义。页面存在多个
同向列表时，用 clean tree 中的稳定容器 label 或 `--container-index` 固定目标，并在
动作后重新 dump 或截图验证。

## Screenshot

```bash
android-use screenshot /tmp/current.png
```

截图保存原生 PNG，模型直接查看画面；JSON 输出提供路径、尺寸和截图耗时。

## Capture

```bash
android-use capture --fps 7 --duration 3s
android-use capture \
  --trigger 'text-gone:正在加载' \
  --before 500ms --after 3s
```

`capture` 输出时间戳 PNG 帧、manifest、contact sheet、中间/结束帧和同期日志。
manifest 的 `actualFps` 是启动采样频率，`deliveryFps` 是帧完成吞吐；验收以有效帧和
实际画面为准。contact sheet 不可用时仍保留 PNG 帧和 manifest。
contact sheet 是可选的 macOS 增强，通过 `xcrun swiftc` 从源码编译，并缓存到
`~/Library/Caches/android-use/`；其他主机仍可录帧并直接查看 PNG。

## 自动失败证据

目标点击/滚动失败、等待超时、动作验证失败和 DeepLink 断言失败会输出
`evidence=<directory>/diagnostics.json`。其中尽可能包含：

- 当前 Activity、分辨率和方向
- 匹配候选与可点击节点 bounds
- clean tree 和 raw XML
- 原生截图
- 当前持续 logcat 路径和健康状态

先读取 diagnostics，再决定是否补命令。参数错误和 raw ADB 错误不会重复采集证据。

# android-use DeepLink 参考

由 App 项目提供 URI 和断言。android-use 负责安全传递 URI，
并可组合 Activity、日志或 UI 断言：

```bash
android-use deeplink 'example://screen/<key>' --wait
android-use deeplink 'example://screen/<key>' \
  --wait \
  --expect-activity '^com\.example\.app/' \
  --assert-timeout 15s
```

可用断言：

```bash
--expect-activity REGEX
--expect-log REGEX
--expect-text TEXT
```

- 无断言时，成功只表示 Android Intent 调度成功，业务验证状态为 `unknown`。
- `--expect-log` 需要动作前已有健康的持续 logcat。
- `--expect-text` 依赖 stock UI tree；Lynx/自绘页面应改用 Activity、日志或后续截图。
- 断言失败自动生成 evidence；有意探测失败路径时可加 `--no-evidence`。

只需要系统 Intent 时，直接使用 [ADB DeepLink](#deeplink)。

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

# android-use Proxy 参考

proxy 是可选能力，要求本机已有 `mitmdump`：

```bash
android-use proxy doctor -s SERIAL
```

工具直接检测可执行文件；不在 `PATH` 时设置
`ANDROID_USE_MITMDUMP=/path/to/mitmdump`。缺少 mitmdump 不影响其他命令。

## 抓包

```bash
android-use proxy start -s SERIAL --port 18081
android-use proxy status -s SERIAL
android-use proxy dump -s SERIAL --format json
android-use proxy dump -s SERIAL --format har -o /tmp/capture.har
android-use proxy stop -s SERIAL --port 18081
```

默认链路是设备宿主机 `127.0.0.1:PORT`、`adb reverse` 和设备全局代理
`127.0.0.1:PORT`。`stop` 恢复启动前的设备代理并移除 reverse。settings 请求在 App
启动前开 proxy；AI 请求在触发生成前开。

## HTTPS

```bash
android-use proxy cert status -s SERIAL
android-use proxy cert instructions -s SERIAL
android-use proxy cert push -s SERIAL
```

量产设备通常需要在系统设置手工安装用户 CA；debug App 还要信任用户证书。证书
pinning 可能继续阻止解密。`install-root` 只适用于明确授权的 root 测试设备，不会由
`proxy start` 自动执行。

## Mock

```bash
android-use proxy start -s SERIAL --mock rules.json
android-use proxy mock -s SERIAL rules2.json
```

规则按 URL 子串顺序匹配，首个命中直接返回指定响应：

```json
[
  {"url": "/v1/example", "status": 200, "body": "{\"items\":[]}"}
]
```

URL 不要写得过宽。Mock 是主动改变服务响应的测试能力，结论仍需结合 UI、logcat 和
请求记录。

媒体注入不再由 android-use 包装；使用
[ADB 文件和媒体命令](#截图和文件)，并只操作隔离测试目录。
