import AppKit
import Foundation

struct CLIError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

struct Frame: Decodable {
    let path: String
    let elapsedMs: Double
}

struct ContactSheetInput: Decodable {
    let frames: [Frame]
}

func argument(_ name: String, in arguments: [String]) throws -> String {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        throw CLIError(message: "\(name) requires a value")
    }
    return arguments[index + 1]
}

func contactSheet(inputPath: String, outputPath: String) throws {
    let data = try Data(contentsOf: URL(fileURLWithPath: inputPath))
    let input = try JSONDecoder().decode(ContactSheetInput.self, from: data)
    guard !input.frames.isEmpty else {
        throw CLIError(message: "contact sheet requires at least one frame")
    }
    guard let first = NSImage(contentsOfFile: input.frames[0].path) else {
        throw CLIError(message: "cannot read first frame: \(input.frames[0].path)")
    }

    let tileWidth = 240
    let tileHeight = max(1, Int((Double(tileWidth) * first.size.height / first.size.width).rounded()))
    let labelHeight = 24
    let columns = min(4, max(1, Int(ceil(sqrt(Double(input.frames.count))))))
    let rows = Int(ceil(Double(input.frames.count) / Double(columns)))
    let width = columns * tileWidth
    let height = rows * (tileHeight + labelHeight)

    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: width,
        pixelsHigh: height,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw CLIError(message: "cannot allocate contact sheet")
    }
    guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw CLIError(message: "cannot create contact sheet graphics context")
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    NSColor.white.setFill()
    NSRect(x: 0, y: 0, width: width, height: height).fill()
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
        .foregroundColor: NSColor.black,
    ]

    for (index, frame) in input.frames.enumerated() {
        guard let image = NSImage(contentsOfFile: frame.path) else {
            throw CLIError(message: "cannot read frame: \(frame.path)")
        }
        let column = index % columns
        let row = index / columns
        let x = column * tileWidth
        let y = height - (row + 1) * (tileHeight + labelHeight)
        image.draw(
            in: NSRect(x: x, y: y + labelHeight, width: tileWidth, height: tileHeight),
            from: NSRect(origin: .zero, size: image.size),
            operation: .copy,
            fraction: 1.0
        )
        let label = String(format: "%.3fs", frame.elapsedMs / 1000.0)
        (label as NSString).draw(
            at: NSPoint(x: x + 5, y: y + 5),
            withAttributes: attributes
        )
    }
    NSGraphicsContext.restoreGraphicsState()

    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw CLIError(message: "cannot encode contact sheet PNG")
    }
    try png.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
}

do {
    let arguments = Array(CommandLine.arguments.dropFirst())
    if arguments.contains("--version") {
        print("android-use-image 1.0.0")
        exit(0)
    }
    guard arguments.first == "contact-sheet" else {
        throw CLIError(
            message: "usage: android-use-image contact-sheet --input JSON --out PNG"
        )
    }
    try contactSheet(
        inputPath: argument("--input", in: arguments),
        outputPath: argument("--out", in: arguments)
    )
} catch {
    FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
    exit(2)
}
