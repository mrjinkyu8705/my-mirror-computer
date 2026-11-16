import { normalizePointerToContent } from '@mirror/protocol'
import { type RefObject, useEffect, useRef } from 'react'

type PointerButton = 'left' | 'right' | 'middle'
type KeyAction = 'down' | 'up'

export interface InputSenders {
  readonly releaseRemoteInput: () => void
  readonly sendKey: (code: string, action: KeyAction) => void
  readonly sendPointerButton: (button: PointerButton, action: KeyAction) => void
  readonly sendPointerMove: (x: number, y: number) => void
  readonly sendPointerWheel: (deltaX: number, deltaY: number) => void
}

// Same whitelist as the protocol control schema; unlisted keys (incl. Meta/Win)
// are ignored so the browser keeps its own shortcuts and nothing extra is sent.
const KEY_CODE_PATTERN =
  /^(?:Key[A-Z]|Digit[0-9]|Numpad(?:[0-9]|Add|Subtract|Multiply|Divide|Decimal|Enter|Equal|Comma)|NumLock|CapsLock|Arrow(?:Up|Down|Left|Right)|F(?:[1-9]|1[0-2])|Backspace|Tab|Enter|Escape|Space|Delete|Home|End|PageUp|PageDown|Shift(?:Left|Right)|Control(?:Left|Right)|Alt(?:Left|Right)|Minus|Equal|BracketLeft|BracketRight|Backslash|Semicolon|Quote|Backquote|Comma|Period|Slash|Lang[12])$/u

const DOM_BUTTON: Record<number, PointerButton> = {
  0: 'left',
  1: 'middle',
  2: 'right',
}

const WHEEL_LIMIT = 1200

// Touch (mobile mode): a stationary hold this long becomes a right click; any
// movement beyond the threshold first becomes a left-button drag instead.
const LONG_PRESS_MS = 2000
const LONG_PRESS_DRAG_THRESHOLD_PX = 12

function getRemoteKeyCode(event: KeyboardEvent): string {
  // Korean 103/106-key keyboards aren't reported consistently across browsers:
  // Chromium may expose the physical 한/영 key as Lang1, AltRight+HangulMode,
  // or an empty/Unidentified code with HangulMode as the logical key. Prefer
  // the logical IME key so all variants toggle the remote Windows IME.
  if (event.key === 'HangulMode' || event.key === 'Hangul') {
    return 'Lang1'
  }
  if (event.key === 'HanjaMode' || event.key === 'Hanja') {
    return 'Lang2'
  }
  return event.code
}

function clampWheel(value: number): number {
  return Math.max(-WHEEL_LIMIT, Math.min(WHEEL_LIMIT, value))
}

/**
 * Capture pointer/keyboard input over the video element and forward it as
 * control messages while control is active. Uses a latest-ref for the senders
 * so listeners attach once per active session (not on every render), and always
 * releases all remote input on cleanup (blur, tab hide, teardown).
 */
export function useInputCapture(
  videoRef: RefObject<HTMLVideoElement | null>,
  isControlActive: boolean,
  senders: InputSenders,
): void {
  const sendersRef = useRef(senders)
  useEffect(() => {
    sendersRef.current = senders
  })

  useEffect(() => {
    const video = videoRef.current
    if (!isControlActive || !video) {
      return
    }

    const toNormalized = (clientX: number, clientY: number) => {
      if (!video.videoWidth || !video.videoHeight) {
        return null
      }
      const rect = video.getBoundingClientRect()
      return normalizePointerToContent(
        clientX - rect.left,
        clientY - rect.top,
        rect.width,
        rect.height,
        video.videoWidth,
        video.videoHeight,
      )
    }

    // Touch gesture state (single active touch). A touch begins undecided
    // ('pending'): movement makes it a left-button drag, a stationary 2s hold
    // becomes a right click, and releasing while still pending is a tap (left
    // click). Mouse pointers bypass this entirely.
    let touchPhase: 'pending' | 'dragging' | 'converted' | null = null
    let touchOrigin: { x: number; y: number } | null = null
    let longPressTimer: number | null = null
    const forwardedKeys = new Set<string>()
    let synthesizedShift: 'ShiftLeft' | null = null
    let synthesizedControl: 'ControlLeft' | null = null
    let pointerFrame: number | null = null
    let pendingPointerMove: { x: number; y: number } | null = null
    let wheelTimer: number | null = null
    let wheelX = 0
    let wheelY = 0
    const clearWheel = () => {
      if (wheelTimer !== null) window.clearTimeout(wheelTimer)
      wheelTimer = null
      wheelX = wheelY = 0
    }
    const flushWheel = (discardRemainder = false) => {
      if (wheelTimer !== null) window.clearTimeout(wheelTimer)
      wheelTimer = null
      // Preserve sub-unit trackpad movement between batches; only integers go
      // over the protocol. A modifier/action/direction boundary discards the
      // old fractional tail so it cannot be applied to the next gesture.
      const x = Math.trunc(wheelX)
      const y = Math.trunc(wheelY)
      wheelX = discardRemainder ? 0 : wheelX - x
      wheelY = discardRemainder ? 0 : wheelY - y
      if (x || y) sendersRef.current.sendPointerWheel(x, y)
    }

    const flushPointerMove = () => {
      if (pointerFrame !== null) {
        window.cancelAnimationFrame(pointerFrame)
        pointerFrame = null
      }
      const point = pendingPointerMove
      pendingPointerMove = null
      if (point) {
        sendersRef.current.sendPointerMove(point.x, point.y)
      }
    }

    const queuePointerMove = (x: number, y: number) => {
      pendingPointerMove = { x, y }
      if (pointerFrame === null) {
        pointerFrame = window.requestAnimationFrame(() => {
          pointerFrame = null
          const point = pendingPointerMove
          pendingPointerMove = null
          if (point) {
            sendersRef.current.sendPointerMove(point.x, point.y)
          }
        })
      }
    }

    const sendPointerMoveImmediately = (x: number, y: number) => {
      pendingPointerMove = null
      if (pointerFrame !== null) {
        window.cancelAnimationFrame(pointerFrame)
        pointerFrame = null
      }
      sendersRef.current.sendPointerMove(x, y)
    }

    const clearLongPressTimer = () => {
      if (longPressTimer !== null) {
        window.clearTimeout(longPressTimer)
        longPressTimer = null
      }
    }

    const resetTouchGesture = () => {
      clearLongPressTimer()
      touchPhase = null
      touchOrigin = null
    }

    const handlePointerMove = (event: PointerEvent) => {
      if (event.pointerType === 'touch') {
        if (touchPhase === 'pending' && touchOrigin) {
          const distance = Math.hypot(
            event.clientX - touchOrigin.x,
            event.clientY - touchOrigin.y,
          )
          if (distance <= LONG_PRESS_DRAG_THRESHOLD_PX) {
            // Undecided: hold the cursor where the finger landed so a drag grabs
            // exactly that point, and a stationary 2s hold can still right click.
            return
          }
          // Finger travelled: commit to a left-button drag. The cursor is already
          // at the touch point (from pointerdown), so pressing now grabs there.
          clearLongPressTimer()
          touchPhase = 'dragging'
          sendersRef.current.sendPointerButton('left', 'down')
        }
        if (touchPhase !== 'dragging') {
          // A tap still forming, or a right click already fired: stream no moves.
          return
        }
      }
      const point = toNormalized(event.clientX, event.clientY)
      if (point) {
        // High-polling-rate mice can emit hundreds of events per second. Only
        // the latest position per browser frame is useful to the remote cursor.
        queuePointerMove(point.x, point.y)
      }
    }

    const handlePointerDown = (event: PointerEvent) => {
      flushWheel(true)
      const button = DOM_BUTTON[event.button]
      if (!button) {
        return
      }
      event.preventDefault()
      // Pull keyboard focus onto the video (tabIndex=-1) so keystrokes are
      // forwarded to the remote instead of activating a focused toolbar button
      // (e.g. Enter triggering the disconnect button).
      video.focus()
      try {
        video.setPointerCapture(event.pointerId)
      } catch {
        // Pointer capture can fail if the pointer was already cancelled.
      }
      // Move first so the agent clicks at the current pointer position.
      const point = toNormalized(event.clientX, event.clientY)
      if (point) {
        // A click must never wait behind a queued move or use stale coordinates.
        sendPointerMoveImmediately(point.x, point.y)
      }
      if (event.pointerType === 'touch' && button === 'left') {
        touchPhase = 'pending'
        touchOrigin = { x: event.clientX, y: event.clientY }
        clearLongPressTimer()
        longPressTimer = window.setTimeout(() => {
          longPressTimer = null
          if (touchPhase === 'pending') {
            touchPhase = 'converted'
            sendersRef.current.sendPointerButton('right', 'down')
            sendersRef.current.sendPointerButton('right', 'up')
          }
        }, LONG_PRESS_MS)
        return
      }
      sendersRef.current.sendPointerButton(button, 'down')
    }

    const handlePointerUp = (event: PointerEvent) => {
      flushWheel(true)
      // Finish a drag at the newest queued position before releasing the button.
      flushPointerMove()
      const button = DOM_BUTTON[event.button]
      if (event.pointerType === 'touch' && touchPhase !== null) {
        if (touchPhase === 'pending') {
          // Released before the long-press window: a tap = left click.
          sendersRef.current.sendPointerButton('left', 'down')
          sendersRef.current.sendPointerButton('left', 'up')
        } else if (touchPhase === 'dragging') {
          sendersRef.current.sendPointerButton('left', 'up')
        }
        // 'converted' already right-clicked; nothing further on release.
        resetTouchGesture()
      } else if (button) {
        sendersRef.current.sendPointerButton(button, 'up')
      }
      if (video.hasPointerCapture(event.pointerId)) {
        video.releasePointerCapture(event.pointerId)
      }
    }

    const handlePointerCancel = (event: PointerEvent) => {
      clearWheel()
      resetTouchGesture()
      if (video.hasPointerCapture(event.pointerId)) {
        video.releasePointerCapture(event.pointerId)
      }
      releaseAll()
    }

    const handleWheel = (event: WheelEvent) => {
      event.preventDefault()
      if (!Number.isFinite(event.deltaX) || !Number.isFinite(event.deltaY)) return
      // Preserve today's pixel-wheel sensitivity. A conventional three-line
      // notch maps to Windows' 120 wheel units; page units scale to the visible
      // video viewport. Never assume all browsers/devices report pixels.
      const rect = event.deltaMode === 2 ? video.getBoundingClientRect() : null
      const scaleX = event.deltaMode === 1 ? 40 : rect ? Math.max(1, rect.width) : 1
      const scaleY = event.deltaMode === 1 ? 40 : rect ? Math.max(1, rect.height) : 1
      const x = event.deltaX * scaleX
      const y = -event.deltaY * scaleY
      if ((wheelX * x < 0) || (wheelY * y < 0)) flushWheel(true)
      // Browser deltaY>0 is scroll-down; Windows wheel positive is up -> negate.
      // Bound high-rate trackpads to about 30 messages/s, leaving action budget
      // for keys/clicks. Keep the protocol's per-message safety clamp.
      wheelX = clampWheel(wheelX + x)
      wheelY = clampWheel(wheelY + y)
      if (wheelTimer === null) wheelTimer = window.setTimeout(() => flushWheel(), 34)
    }

    const handleContextMenu = (event: MouseEvent) => {
      event.preventDefault()
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      const remoteCode = getRemoteKeyCode(event)
      if (
        event.repeat ||
        isInteractiveTarget(event.target) ||
        !KEY_CODE_PATTERN.test(remoteCode)
      ) {
        return
      }
      event.preventDefault()

      flushWheel(true)
      // Ctrl may have gone down over a local panel, or its standalone event
      // may not have reached this viewer. The chord event carries its state:
      // repair only ordinary Ctrl chords, not AltGr or local Meta shortcuts.
      if (event.ctrlKey === false && !remoteCode.startsWith('Control')) {
        for (const controlCode of ['ControlLeft', 'ControlRight'] as const) {
          if (forwardedKeys.has(controlCode)) {
            sendersRef.current.sendKey(controlCode, 'up')
            forwardedKeys.delete(controlCode)
          }
        }
        synthesizedControl = null
      }
      if (
        event.ctrlKey === true &&
        !event.altKey && !event.metaKey && !event.getModifierState?.('AltGraph') &&
        !remoteCode.startsWith('Control') &&
        !forwardedKeys.has('ControlLeft') && !forwardedKeys.has('ControlRight')
      ) {
        sendersRef.current.sendKey('ControlLeft', 'down')
        forwardedKeys.add('ControlLeft')
        synthesizedControl = 'ControlLeft'
      }
      // If a Shift keyup was lost (for example while focus was changing), the
      // next unshifted keyboard event is authoritative: release any Shift that
      // this viewer still considers pressed before forwarding the new key.
      if (!event.shiftKey && !remoteCode.startsWith('Shift')) {
        for (const shiftCode of ['ShiftLeft', 'ShiftRight'] as const) {
          if (forwardedKeys.has(shiftCode)) {
            sendersRef.current.sendKey(shiftCode, 'up')
            forwardedKeys.delete(shiftCode)
          }
        }
        synthesizedShift = null
      }

      // Some browser/IME combinations omit the standalone Shift keydown while
      // still marking the following printable key with shiftKey=true. Repair
      // that lost modifier before forwarding the key so Shift+A and Shift+1
      // remain an ordered remote chord.
      if (
        event.shiftKey &&
        !remoteCode.startsWith('Shift') &&
        !forwardedKeys.has('ShiftLeft') &&
        !forwardedKeys.has('ShiftRight')
      ) {
        sendersRef.current.sendKey('ShiftLeft', 'down')
        forwardedKeys.add('ShiftLeft')
        synthesizedShift = 'ShiftLeft'
      }
      sendersRef.current.sendKey(remoteCode, 'down')
      forwardedKeys.add(remoteCode)
    }

    const handleKeyUp = (event: KeyboardEvent) => {
      const remoteCode = getRemoteKeyCode(event)
      if (!KEY_CODE_PATTERN.test(remoteCode)) {
        return
      }

      // Always release a key that was forwarded, even if focus moved onto a
      // toolbar control between keydown and keyup. Ignoring that keyup would
      // leave Shift (or another modifier) stuck on the remote computer.
      const isShiftRelease = remoteCode.startsWith('Shift')
      const isControlRelease = remoteCode.startsWith('Control')
      const wasForwarded = forwardedKeys.has(remoteCode)
      if (isInteractiveTarget(event.target) && !wasForwarded && !isShiftRelease &&
        !(isControlRelease && synthesizedControl !== null)) {
        return
      }
      event.preventDefault()

      flushWheel(true)
      // A synthesized left Ctrl can originate from either physical Ctrl key.
      // Its release must still reach the remote after focus moves to a panel.
      if (isControlRelease && synthesizedControl !== null && synthesizedControl !== remoteCode) {
        sendersRef.current.sendKey(synthesizedControl, 'up')
        forwardedKeys.delete(synthesizedControl)
        synthesizedControl = null
      }
      // A repaired ShiftLeft may correspond to a physical ShiftRight release.
      // Release the synthesized key first so the remote modifier cannot stick.
      if (
        isShiftRelease &&
        synthesizedShift !== null &&
        synthesizedShift !== remoteCode
      ) {
        sendersRef.current.sendKey(synthesizedShift, 'up')
        forwardedKeys.delete(synthesizedShift)
        synthesizedShift = null
      }
      sendersRef.current.sendKey(remoteCode, 'up')
      forwardedKeys.delete(remoteCode)
      if (synthesizedControl === remoteCode) {
        synthesizedControl = null
      } else if (
        synthesizedControl !== null && !/^(?:Control|Shift|Alt)/u.test(remoteCode) &&
        ![...forwardedKeys].some((code) => !/^(?:Control|Shift|Alt)/u.test(code))
      ) {
        // Bound the repair to the current chord even if Ctrl keyup is lost.
        // Keep Ctrl until all chord keys are released (including Ctrl+Shift).
        sendersRef.current.sendKey(synthesizedControl, 'up')
        forwardedKeys.delete(synthesizedControl)
        synthesizedControl = null
      }
      if (synthesizedShift === remoteCode) {
        synthesizedShift = null
      } else if (!isShiftRelease && synthesizedShift !== null) {
        // A synthesized Shift belongs only to this repaired chord. Releasing it
        // with the character/digit prevents a missing physical Shift keyup from
        // leaving every later remote keystroke shifted.
        sendersRef.current.sendKey(synthesizedShift, 'up')
        forwardedKeys.delete(synthesizedShift)
        synthesizedShift = null
      }
    }

    const releaseAll = () => {
      clearWheel()
      pendingPointerMove = null
      if (pointerFrame !== null) window.cancelAnimationFrame(pointerFrame)
      pointerFrame = null
      sendersRef.current.releaseRemoteInput()
      forwardedKeys.clear()
      synthesizedShift = null
      synthesizedControl = null
    }

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        releaseAll()
      }
    }

    video.addEventListener('pointermove', handlePointerMove)
    video.addEventListener('pointerdown', handlePointerDown)
    video.addEventListener('pointerup', handlePointerUp)
    video.addEventListener('pointercancel', handlePointerCancel)
    video.addEventListener('wheel', handleWheel, { passive: false })
    video.addEventListener('contextmenu', handleContextMenu)
    window.addEventListener('keydown', handleKeyDown)
    window.addEventListener('keyup', handleKeyUp)
    window.addEventListener('blur', releaseAll)
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      video.removeEventListener('pointermove', handlePointerMove)
      video.removeEventListener('pointerdown', handlePointerDown)
      video.removeEventListener('pointerup', handlePointerUp)
      video.removeEventListener('pointercancel', handlePointerCancel)
      video.removeEventListener('wheel', handleWheel)
      video.removeEventListener('contextmenu', handleContextMenu)
      window.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener('keyup', handleKeyUp)
      window.removeEventListener('blur', releaseAll)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      resetTouchGesture()
      pendingPointerMove = null
      clearWheel()
      if (pointerFrame !== null) {
        window.cancelAnimationFrame(pointerFrame)
        pointerFrame = null
      }
      // Ensure nothing stays pressed when control ends.
      sendersRef.current.releaseRemoteInput()
    }
  }, [isControlActive, videoRef])
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) {
    return false
  }
  return (
    (target instanceof HTMLElement && target.isContentEditable) ||
    Boolean(target.closest('button, input, select, textarea, summary, a[href], [role="textbox"], [role="toolbar"], [role="dialog"], [data-local-input]'))
  )
}
