# android-use UI 参考

本文件只描述 android-use 保留的 UI 语义和视觉能力。App、按键、基础输入和文件
操作见 [ADB 基础操作](adb.md)。

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
