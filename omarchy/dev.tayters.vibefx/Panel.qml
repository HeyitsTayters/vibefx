import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// VibeFX bar widget. All state comes from one long-lived `vibefx ctl watch`
// process: it prints the app's state as a JSON line whenever it changes (or
// VibeFX starts/quits) and takes ctl commands on stdin, so slider drags don't
// spawn a process per step.
Panel {
  id: root
  moduleName: "dev.tayters.vibefx"
  ipcTarget: "dev.tayters.vibefx"
  manageIpc: false

  // Last state from VibeFX, patched optimistically when we send a command so
  // controls don't snap back while the round trip is in flight.
  property var vibe: ({})
  property bool installed: true

  readonly property bool running: vibe.running === true
  readonly property bool known: vibe.presets !== undefined
  readonly property bool enabled: vibe.enabled === true
  readonly property string preset: vibe.preset || ""
  readonly property var presets: vibe.presets || []
  readonly property var effectInfo: vibe.effectInfo || []
  readonly property bool limiter: vibe.limiter !== false

  readonly property string glyphOn: String.fromCodePoint(0xf0ea2)   // md-equalizer
  readonly property string glyphOff: String.fromCodePoint(0xf0ea3)  // md-equalizer_outline

  readonly property string statusText: {
    if (!installed) return "Not installed"
    if (!known) return "Connecting…"
    if (!running) return "Not running"
    return (enabled ? "On" : "Off") + " · " + preset
  }

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dimFg: Qt.darker(fg, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: Style.hoverFillFor(fg, Color.accent)
  readonly property color selectedFill: Style.selectedFillFor(fg, Color.accent)

  property real wheelAccumulator: 0

  // ---- commands

  function send(line) {
    if (watcher.running) watcher.write(line + "\n")
  }

  function patch(changes) {
    var next = Object.assign({}, vibe, changes)
    vibe = next
  }

  function setEnabled(on) {
    if (!running) { start(); return }
    patch({ enabled: on })
    send(on ? "on" : "off")
  }

  function toggleEnabled() { setEnabled(!enabled) }

  function applyPreset(name) {
    if (!running || presets.indexOf(name) < 0) return
    patch({ preset: name })
    send("preset " + name)
  }

  function cyclePreset(steps) {
    if (!running || presets.length === 0) return
    var i = presets.indexOf(preset)
    if (i < 0) i = steps > 0 ? -1 : 0
    applyPreset(presets[((i + steps) % presets.length + presets.length) % presets.length])
  }

  function setEffect(key, value) {
    if (!running) return
    var v = Math.max(0, Math.min(10, Math.round(value)))
    var effects = Object.assign({}, vibe.effects || {})
    if (effects[key] === v) return
    effects[key] = v
    patch({ effects: effects, preset: "Custom" })
    send("effect " + key + " " + v)
  }

  function effectValue(key) {
    return vibe.effects && vibe.effects[key] !== undefined ? vibe.effects[key] : 0
  }

  function effectLocked(key) { return key === "boost" && !limiter }

  function start() { send("start") }
  function showApp() { send("show"); close() }
  function quitApp() { send("quit") }

  // ---- keyboard / hover cursor
  // One flat list of targets: the power switch, presets (a two-column grid),
  // effect sliders, then the app actions.

  property bool cursorActive: false
  property int cursorIndex: 0

  readonly property var cursorItems: {
    var list = [{ kind: "power" }]
    if (running) {
      for (var i = 0; i < presets.length; i++) list.push({ kind: "preset", key: presets[i], pos: i })
      for (var j = 0; j < effectInfo.length; j++) list.push({ kind: "effect", key: effectInfo[j].key })
      list.push({ kind: "open" })
      list.push({ kind: "quit" })
    } else if (installed) {
      list.push({ kind: "start" })
    }
    return list
  }

  function indexOfItem(kind, key) {
    for (var i = 0; i < cursorItems.length; i++)
      if (cursorItems[i].kind === kind && (key === undefined || cursorItems[i].key === key)) return i
    return -1
  }

  function hasCursor(kind, key) {
    return cursorActive && cursorIndex === indexOfItem(kind, key)
  }

  function hoverCursor(kind, key) {
    var i = indexOfItem(kind, key)
    if (i < 0) return
    cursorActive = true
    cursorIndex = i
  }

  function currentItem() {
    return cursorItems[Math.max(0, Math.min(cursorItems.length - 1, cursorIndex))]
  }

  function moveCursor(dx, dy) {
    if (!cursorActive) { cursorActive = true; return }
    var item = currentItem()
    if (dx !== 0 && item.kind === "effect") {
      setEffect(item.key, effectValue(item.key) + dx)
      return
    }
    var step = dy
    // Presets sit in two columns: up/down jump a row, left/right a column.
    if (item.kind === "preset") {
      if (dx !== 0) step = dx
      else if (dy > 0 && item.pos + 2 < presets.length) step = 2
      else if (dy < 0 && item.pos >= 2) step = -2
      else if (dy > 0) step = presets.length - item.pos
      else step = -item.pos - 1
    } else if (dx !== 0) {
      return
    }
    cursorIndex = Math.max(0, Math.min(cursorItems.length - 1, cursorIndex + step))
  }

  function activateCursor() {
    if (!cursorActive) return
    var item = currentItem()
    if (item.kind === "power") toggleEnabled()
    else if (item.kind === "preset") applyPreset(item.key)
    else if (item.kind === "open") showApp()
    else if (item.kind === "quit") quitApp()
    else if (item.kind === "start") start()
  }

  onCursorItemsChanged: if (cursorIndex >= cursorItems.length) cursorIndex = cursorItems.length - 1

  onOpenedChanged: if (opened) { cursorActive = false; cursorIndex = 0 }

  // ---- connection to VibeFX

  Process {
    id: watcher
    command: ["sh", "-c", "exec \"$(command -v vibefx || echo \"$HOME/.local/bin/vibefx\")\" ctl watch"]
    running: true
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        try {
          root.vibe = JSON.parse(line)
          root.installed = true
        } catch (e) {
          console.warn("vibefx: bad state line:", line)
        }
      }
    }
    onExited: function(code) {
      // 127: sh couldn't find vibefx. Anything else: retry shortly.
      root.installed = code !== 127
      root.vibe = ({})
      retry.restart()
    }
  }

  Timer {
    id: retry
    interval: 5000
    onTriggered: watcher.running = true
  }

  IpcHandler {
    target: "dev.tayters.vibefx"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function toggleEffects(): string { root.toggleEnabled(); return root.statusText }
    function on(): string { root.setEnabled(true); return root.statusText }
    function off(): string { root.setEnabled(false); return root.statusText }
    function preset(name: string): string { root.applyPreset(name); return root.statusText }
    function nextPreset(): string { root.cyclePreset(1); return root.statusText }
    function previousPreset(): string { root.cyclePreset(-1); return root.statusText }
    function status(): string { return JSON.stringify(root.vibe) }
  }

  // ---- bar button

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.running && root.enabled ? root.glyphOn : root.glyphOff
    dimmed: !root.running
    tooltipText: "VibeFX · " + root.statusText
    onPressed: function(b) {
      if (b === Qt.RightButton) root.toggleEnabled()
      else if (b === Qt.MiddleButton) root.showApp()
      else root.toggle()
    }
    onWheelMoved: function(delta) {
      var wheel = Util.wheelSteps(root.wheelAccumulator, delta)
      root.wheelAccumulator = wheel.remainder
      if (wheel.steps !== 0) root.cyclePreset(-wheel.steps)
    }
  }

  // ---- popup

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(720))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "o" || t === "O") root.showApp()
        else if (t === "p" || t === "P") root.toggleEnabled()
      }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panelColumn.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        Binding {
          target: scrollArea.contentItem
          property: "interactive"
          value: panelColumn.implicitHeight > scrollArea.height
        }

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.space(14)

          // ---------- Hero: icon · title/status · power ----------
          PanelHero {
            width: parent.width
            title: "VibeFX"
            meta: root.statusText
            foreground: root.fg
            fontFamily: root.fontFamily
            iconOpacity: root.running && root.enabled ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.running && root.enabled ? root.glyphOn : root.glyphOff
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              ToggleSwitch {
                id: powerSwitch
                visible: root.installed
                checked: root.running && root.enabled
                hasCursor: root.hasCursor("power")
                foreground: root.fg
                onHovered: function(on) { if (on) root.hoverCursor("power") }
                onToggled: root.toggleEnabled()

                PanelToolTip {
                  visible: powerSwitch.containsMouse
                  text: !root.running ? "Start VibeFX" : (root.enabled ? "Bypass effects" : "Enable effects")
                  fontFamily: root.fontFamily
                }
              }
            }
          }

          // ---------- Not running / not installed ----------
          Text {
            visible: !root.installed
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "The vibefx command wasn't found. Install VibeFX with its install.sh."
            color: root.dimFg
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          ActionRow {
            visible: root.installed && root.known && !root.running
            kind: "start"
            glyph: String.fromCodePoint(0xf040a)  // md-play
            label: "Start VibeFX"
            onActivated: root.start()
          }

          // ---------- Presets ----------
          PanelSeparator { visible: root.running; foreground: root.fg }

          Column {
            visible: root.running
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: root.preset === "Custom" ? "PRESET · CUSTOM" : "PRESET"
              foreground: root.fg
              fontFamily: root.fontFamily
            }

            Grid {
              id: presetGrid
              width: parent.width
              columns: 2
              columnSpacing: Style.space(6)
              rowSpacing: Style.space(4)

              Repeater {
                model: root.presets

                CursorSurface {
                  id: presetChip
                  required property string modelData
                  readonly property bool isActive: root.preset === modelData
                  width: (presetGrid.width - presetGrid.columnSpacing) / 2
                  implicitHeight: presetLabel.implicitHeight + Style.spacing.xl
                  hasCursor: root.hasCursor("preset", modelData)
                  current: isActive
                  foreground: root.fg
                  fill: root.hoverFill
                  currentFill: root.selectedFill

                  Text {
                    id: presetLabel
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: Style.space(10)
                    anchors.rightMargin: Style.space(6)
                    anchors.verticalCenter: parent.verticalCenter
                    textFormat: Text.PlainText
                    text: presetChip.modelData
                    color: root.fg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: presetChip.isActive
                    elide: Text.ElideRight
                  }

                  MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onContainsMouseChanged: if (containsMouse) root.hoverCursor("preset", presetChip.modelData)
                    onClicked: root.applyPreset(presetChip.modelData)
                  }
                }
              }
            }
          }

          // ---------- Effects ----------
          PanelSeparator { visible: root.running; foreground: root.fg }

          Column {
            visible: root.running
            width: parent.width
            spacing: Style.space(4)

            PanelSectionHeader {
              text: "EFFECTS"
              foreground: root.fg
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.effectInfo

              CursorSurface {
                id: effectRow
                required property var modelData
                readonly property string key: modelData.key
                readonly property bool locked: root.effectLocked(key)
                width: panelColumn.width
                implicitHeight: Math.max(effectLabel.implicitHeight, slider.implicitHeight) + Style.space(4)
                hasCursor: root.hasCursor("effect", key)
                foreground: root.fg
                outline: true

                // One line per effect: label · slider · value.
                Text {
                  id: effectLabel
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.round(parent.width * 0.36)
                  textFormat: Text.PlainText
                  text: effectRow.modelData.label
                  color: root.fg
                  opacity: effectRow.locked ? 0.5 : 1.0
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  elide: Text.ElideRight

                  HoverHandler { id: effectLabelHover }

                  PanelToolTip {
                    visible: effectLabelHover.hovered
                    text: effectRow.locked
                      ? effectRow.modelData.description + ". Install swh-plugins to enable it."
                      : effectRow.modelData.description
                    fontFamily: root.fontFamily
                  }
                }

                PanelSlider {
                  id: slider
                  bar: root.bar
                  anchors.left: effectLabel.right
                  anchors.right: effectValueText.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  minimum: 0
                  maximum: 10
                  step: 1
                  integer: true
                  tickCount: 11
                  value: root.effectValue(effectRow.key)
                  enabled: !effectRow.locked
                  opacity: effectRow.locked ? 0.4 : (root.enabled ? 1.0 : 0.6)
                  onMoved: function(v) { root.setEffect(effectRow.key, v) }
                }

                Text {
                  id: effectValueText
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(18)
                  horizontalAlignment: Text.AlignRight
                  textFormat: Text.PlainText
                  text: effectRow.locked ? "–" : Math.round(slider.dragging ? slider.liveValue : root.effectValue(effectRow.key))
                  color: root.dimFg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }

                HoverHandler {
                  onHoveredChanged: if (hovered) root.hoverCursor("effect", effectRow.key)
                }
              }
            }
          }

          // ---------- App ----------
          PanelSeparator { visible: root.running; foreground: root.fg }

          Column {
            visible: root.running
            width: parent.width
            spacing: Style.space(2)

            ActionRow {
              kind: "open"
              glyph: String.fromCodePoint(0xf0493)  // md-cog
              label: "Open VibeFX…"
              hint: "EQ, device, headphone correction"
              onActivated: root.showApp()
            }

            ActionRow {
              kind: "quit"
              glyph: String.fromCodePoint(0xf0425)  // md-power
              label: "Quit VibeFX"
              hint: "Stops the enhancement"
              onActivated: root.quitApp()
            }
          }
        }
      }
    }
  }

  component ActionRow: CursorSurface {
    id: actionRow
    property string kind: ""
    property string glyph: ""
    property string label: ""
    property string hint: ""
    signal activated()

    width: panelColumn.width
    implicitHeight: actionInner.implicitHeight + Style.spacing.xl
    hasCursor: root.hasCursor(kind)
    foreground: root.fg
    fill: root.hoverFill

    Row {
      id: actionInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: actionRow.glyph
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.title
        width: Style.space(22)
        horizontalAlignment: Text.AlignHCenter
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        textFormat: Text.PlainText
        text: actionRow.label
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        visible: actionRow.hint !== ""
        textFormat: Text.PlainText
        text: actionRow.hint
        color: root.dimFg
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        width: Math.max(0, actionInner.width - x)
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onContainsMouseChanged: if (containsMouse) root.hoverCursor(actionRow.kind)
      onClicked: actionRow.activated()
    }
  }
}
