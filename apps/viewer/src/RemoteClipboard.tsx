import { useEffect, useRef, useState } from 'react'
import { ActionPanel } from './ActionPanel'
import { CLIPBOARD_TEXT_MAX_CHARS, CLIPBOARD_TEXT_RECEIVE_MAX_CHARS } from '@mirror/protocol'

interface RemoteClipboardProps {
  readonly open: boolean
  readonly canSend: boolean
  readonly setRemoteClipboard: (text: string, onProgress?: (sent: number, total: number) => void) => Promise<void>
  readonly sendKey: (code: string, action: 'down' | 'up') => void
  readonly releaseRemoteInput: () => void
  readonly onClose: () => void
  readonly entries: readonly { id: number; text: string }[]
  readonly dismissEntry: (id: number) => void
  readonly copyText: (text: string) => Promise<boolean>
}

const CLIPBOARD_MAX_CHARS = CLIPBOARD_TEXT_MAX_CHARS

// Mounted for the active control session even while its surface is closed.
// Drafts live only in memory; closing/switching panels does not erase them.
export function RemoteClipboard(props: RemoteClipboardProps) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState('')
  const [tone, setTone] = useState<'info' | 'success' | 'error'>('info')
  const [sending, setSending] = useState(false)
  const sendInFlight = useRef(false)
  const mounted = useRef(true)
  const statusTimer = useRef<number | null>(null)
  const flashStatus = (message: string, kind: typeof tone = 'info') => {
    setStatus(message)
    setTone(kind)
    if (statusTimer.current !== null) window.clearTimeout(statusTimer.current)
    statusTimer.current = window.setTimeout(() => setStatus(''), 4000)
  }
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      if (statusTimer.current !== null) window.clearTimeout(statusTimer.current)
    }
  }, [])

  const pasteToRemote = async () => {
    if (!text || sendInFlight.current || !props.canSend) return
    if (text.length > CLIPBOARD_MAX_CHARS) {
      flashStatus(`${CLIPBOARD_MAX_CHARS.toLocaleString()}자를 초과했습니다. 내용을 나눠 주세요.`, 'error')
      return
    }
    const payload = text
    sendInFlight.current = true
    setSending(true)
    if (statusTimer.current !== null) window.clearTimeout(statusTimer.current)
    setTone('info')
    setStatus('원격 PC에 전송을 준비하고 있습니다…')
    try {
      await props.setRemoteClipboard(payload, (sent, total) => {
        if (mounted.current) setStatus(sent < total ? `텍스트 전송 중… ${Math.round(sent / total * 100)}%` : '원격 클립보드 적용을 확인하고 있습니다…')
      })
      if (!mounted.current) return
      props.sendKey('ControlLeft', 'down')
      props.sendKey('KeyV', 'down')
      props.sendKey('KeyV', 'up')
      props.sendKey('ControlLeft', 'up')
      flashStatus(`클립보드 적용 완료 · 붙여넣기 요청을 보냈습니다 (${payload.length.toLocaleString()}자)`, 'success')
    } catch (error) {
      const code = error instanceof Error ? error.message : ''
      const messages: Record<string, string> = {
        TIMEOUT: '원격 PC의 확인 시간이 초과되었습니다. 내용은 유지됩니다. 다시 시도해 주세요.',
        DISCONNECTED: '연결이 끊겼습니다. 재접속 후 다시 시도해 주세요.',
        CONTROL_INACTIVE: '텍스트 전송 채널이 준비되지 않았습니다. 새로고침 후 재접속해 주세요.',
        INVALID_TEXT: '길이가 초과되었거나 지원하지 않는 문자가 포함되어 있습니다.',
        BUSY: '다른 텍스트가 전송 중입니다.',
        CLIPBOARD_WRITE_FAILED: '원격 클립보드에 쓰지 못했습니다. 잠시 후 다시 시도해 주세요.',
      }
      if (mounted.current) flashStatus(messages[code] ?? '텍스트 전송을 완료하지 못했습니다. 다시 시도해 주세요.', 'error')
    } finally {
      sendInFlight.current = false
      if (mounted.current) setSending(false)
    }
  }

  return (
    <ActionPanel open={props.open} title="텍스트 클립보드" subtitle="접속 PC ↔ 원격 PC"
      className="remote-clipboard" testId="remote-clipboard" closeTestId="remote-clipboard-close"
      onClose={props.onClose} releaseRemoteInput={props.releaseRemoteInput}
      initialFocus="textarea"
      footer={<>
        <div className="action-panel-actions">
          <button type="button" disabled={!text || sending} onClick={() => setText('')}>입력 지우기</button>
          <button className="remote-clipboard-primary" data-testid="remote-clipboard-paste"
            disabled={!text || sending || !props.canSend} onClick={() => void pasteToRemote()} type="button">{sending ? '전송 중…' : '원격 PC에 붙여넣기'}</button>
        </div>
        <p className="action-panel-status" data-testid="remote-clipboard-status"
          data-tone={tone} role="status">{status || '원격 PC의 입력 위치를 먼저 선택해 주세요.'}</p>
      </>}>
      <section className="remote-clipboard-send">
        <h2>접속 PC → 원격 PC</h2>
        <textarea aria-label="원격 PC로 보낼 텍스트" className="remote-clipboard-input"
          data-testid="remote-clipboard-input" maxLength={CLIPBOARD_MAX_CHARS}
          disabled={sending} onChange={(event) => setText(event.target.value)}
          placeholder="입력하거나 Ctrl+V로 텍스트를 붙여넣으세요." rows={5} value={text} />
        <span className="panel-hint">{text.length.toLocaleString()} / {CLIPBOARD_MAX_CHARS.toLocaleString()}자</span>
      </section>
      <details className="text-clipboard-history">
        <summary>원격 PC → 접속 PC · 받은 텍스트 {props.entries.length}개</summary>
        <p className="panel-hint">한 번에 최대 {CLIPBOARD_TEXT_RECEIVE_MAX_CHARS.toLocaleString()}자 · 최근 20개 보관</p>
        {props.entries.length === 0 ? <p className="panel-hint">원격 PC에서 복사한 텍스트가 여기에 표시됩니다.</p> :
          <ul className="clipboard-list">{props.entries.map((entry) => (
            <li className="clipboard-item" key={entry.id}>
              <p className="clipboard-text">{entry.text}</p>
              <div className="clipboard-item-actions">
                <button type="button" onClick={() => {
                  void props.copyText(entry.text).then((copied) => {
                    flashStatus(copied ? '이 컴퓨터의 클립보드에 복사했습니다.' :
                      '복사하지 못했습니다. 브라우저의 클립보드 권한을 확인해 주세요.', copied ? 'success' : 'error')
                  })
                }}>이 컴퓨터에 복사</button>
                <button type="button" aria-label="받은 텍스트 삭제" onClick={() => props.dismissEntry(entry.id)}>×</button>
              </div>
            </li>
          ))}</ul>}
      </details>
    </ActionPanel>
  )
}
