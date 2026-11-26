import AppKit

let canvas = NSSize(width: 1024, height: 1024)
let image = NSImage(size: canvas)
image.lockFocus()
NSColor.clear.setFill()
NSRect(origin: .zero, size: canvas).fill()
let background = NSBezierPath(roundedRect: NSRect(origin: .zero, size: canvas), xRadius: 220, yRadius: 220)
NSColor(calibratedRed: 0.98, green: 0.08, blue: 0.12, alpha: 1).setFill()
background.fill()
let play = NSBezierPath()
play.move(to: NSPoint(x: 395, y: 286))
play.line(to: NSPoint(x: 735, y: 512))
play.line(to: NSPoint(x: 395, y: 738))
play.close()
NSColor.white.setFill()
play.fill()
image.unlockFocus()

guard let data = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: data),
      let png = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("Could not render the app icon.")
}
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
