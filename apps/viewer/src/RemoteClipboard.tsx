import { useEffect, useRef, useState } from 'react'

interface RemoteClipboardProps {
  readonly setRemoteClipboard: (text: string) => void
  readonly sendKey: (code: string, action: 'down' | 'up') => void
  readonly onClose: () => void
}

// Mirrors CLIPBOARD_TEXT_MAX_LENGTH in the protocol control schema.
const CLIPBOARD_MAX_CHARS = 16_384

/**
 * Clipboard surface (company PC -> home PC). The user types or pastes (Ctrl+V)
 * text here, then "붙여넣기 →" writes the home PC clipboard (clipboard.set) and
 * sends a Ctrl+V chord so it lands at the remote cursor. The control channel is
 * ordered/reliable, so clipboard.set is always applied before the paste chord.
 */
export function RemoteClipboard({ setRemoteClipboard, sendKey, onClose }: RemoteClipboardProps) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState<string | null>(null)
  const statusTimer = useRef<number | null>(null)

  const flashStatus = (message: string) => {
    setStatus(message)
    if (statusTimer.current !== null) {
      window.clearTimeout(statusTimer.current)
    }
    statusTimer.current = window.setTimeout(() => setStatus(null), 2500)
  }

  useEffect(
    () => () => {
      if (statusTimer.current !== null) {
        window.clearTimeout(statusTimer.current)
      }
    },
    [],
  )

  const pasteToRemote = () => {
    const payload = text.slice(0, CLIPBOARD_MAX_CHARS)
    if (!payload) {
      return
    }
    setRemoteClipboard(payload)
    // Ctrl+V on the remote. Ordered channel guarantees the clipboard is set
    // before these key events are processed by the agent.
    sendKey('ControlLeft', 'down')
    sendKey('KeyV', 'down')
    sendKey('KeyV', 'up')
    sendKey('ControlLeft', 'up')
    flashStatus(`집 PC에 붙여넣었습니다 (${payload.length}자)`)
  }

  return (
    <aside
      aria-label="텍스트 클립보드"
      className="remote-clipboard"
      data-testid="remote-clipboard"
    >
      <header className="remote-clipboard-head">
        <div>
          <strong>텍스트 클립보드</strong>
          <span>접속 PC → 원격 PC</span>
        </div>
        <button
          aria-label="텍스트 클립보드 닫기"
          data-testid="remote-clipboard-close"
          onClick={onClose}
          type="button"
        >
          ×
        </button>
      </header>

      <section className="remote-clipboard-send">
        <h2>원격 PC에 붙여넣기</h2>
        <textarea
          aria-label="원격 PC로 보낼 텍스트"
          className="remote-clipboard-input"
          data-testid="remote-clipboard-input"
          maxLength={CLIPBOARD_MAX_CHARS}
          onChange={(event) => setText(event.target.value)}
          placeholder="여기에 입력하거나 Ctrl+V로 텍스트를 붙여넣으세요."
          rows={6}
          value={text}
        />
        <div className="remote-clipboard-actions">
          <button
            className="remote-clipboard-primary"
            data-testid="remote-clipboard-paste"
            disabled={text.length === 0}
            onClick={pasteToRemote}
            type="button"
          >
            원격 PC에 붙여넣기
          </button>
        </div>
      </section>

      <p
        className="remote-clipboard-status"
        data-testid="remote-clipboard-status"
        role="status"
      >
        {status}
      </p>
    </aside>
  )
}
