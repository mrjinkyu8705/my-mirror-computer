import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react'

import { ImageClipboardPanel } from './ImageClipboardPanel'
import { ConnectionTimingDetails } from './ConnectionTimingDetails'
import { RemoteClipboard } from './RemoteClipboard'
import { IconButton } from './IconButton'
import { ViewerIcon } from './ViewerIcon'
import { ViewerToolbar } from './ViewerToolbar'
import { FullscreenOverlayLayer } from './FullscreenOverlayLayer'
import { useInputCapture } from './useInputCapture'
import { useRemoteSession } from './useRemoteSession'
import {
  type DisplayMode,
  formatSessionDuration,
  getConnectionQuality,
} from './viewerMetrics'

const STATE_LABELS = {
  closing: '종료 중',
  'control-active': '제어 채널 연결됨',
  negotiating: 'WebRTC 협상 중',
  offline: '오프라인',
  online: '시그널링 연결됨',
  reserved: '세션 예약됨',
  'view-active': '화면 수신 중',
} as const

const VIDEO_PROFILE_LABELS = {
  balanced: 'Balanced',
  high: 'High',
  low: 'Low',
} as const

const CONNECTION_ROUTE_LABELS = {
  direct: '직접 연결',
  turn: 'TURN 중계',
  unknown: '경로 확인 중',
} as const

// Write text to the local clipboard on a user gesture. Prefers the async
// Clipboard API (secure contexts) and falls back to execCommand so it still
// works over plain http on a LAN. Must be called from a click handler.
async function copyTextToClipboard(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the execCommand path
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.append(textarea)
    textarea.select()
    const copied = document.execCommand('copy')
    textarea.remove()
    return copied
  } catch {
    return false
  }
}

// Win+Shift+S (Windows screenshot/snip tool). Press modifiers down in order,
// then release in reverse order so the remote OS sees a well-formed chord.
function sendCaptureShortcut(sendKey: (code: string, action: 'down' | 'up') => void): void {
  sendKey('MetaLeft', 'down')
  sendKey('ShiftLeft', 'down')
  sendKey('KeyS', 'down')
  sendKey('KeyS', 'up')
  sendKey('ShiftLeft', 'up')
  sendKey('MetaLeft', 'up')
}

const FILE_STATUS_LABEL: Record<string, string> = {
  preparing: '준비 중…',
  sending: '전송 중',
  verifying: '검증 중…',
  done: '전송 완료',
  error: '전송 실패',
}

const DOWNLOAD_STATUS_LABEL: Record<string, string> = {
  downloading: '받는 중',
  verifying: '검증 중…',
  done: '받기 완료',
  error: '받기 실패',
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`
  }
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`
}

export function App() {
  const viewerStage = useRef<HTMLElement>(null)
  const videoElement = useRef<HTMLVideoElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const session = useRemoteSession()
  const [displayMode, setDisplayMode] = useState<DisplayMode>('fit')
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [fullscreenToolsOpen, setFullscreenToolsOpen] = useState(false)
  const [toolsPinned, setToolsPinned] = useState(false)
  const [toolsHovered, setToolsHovered] = useState(false)
  const [toolsKeyboardFocus, setToolsKeyboardFocus] = useState(false)
  const [toolsBusy, setToolsBusy] = useState(false)
  const [disconnectConfirmOpen, setDisconnectConfirmOpen] = useState(false)
  const cancelDisconnect = useRef<HTMLButtonElement>(null)
  const disconnectDialog = useRef<HTMLDivElement>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [framesPerSecond, setFramesPerSecond] = useState<number | null>(null)
  const [videoSize, setVideoSize] = useState({ width: 0, height: 0 })
  const [presentationError, setPresentationError] = useState<string | null>(null)
  const [clipboardPanelOpen, setClipboardPanelOpen] = useState(false)
  const [imageClipboardPanelOpen, setImageClipboardPanelOpen] = useState(false)
  const [downloadPanelOpen, setDownloadPanelOpen] = useState(false)
  const [copiedClipboardId, setCopiedClipboardId] = useState<number | null>(null)
  const lastReceivedImageId = useRef(0)
  const [imageUnread, setImageUnread] = useState(false)
  const newestImageId = session.clipboardImageEntries[0]?.id ?? 0

  useEffect(() => {
    // Track actual arrival IDs, not list length or another panel's close event.
    // IDs also detect new images when the six-entry history is already full.
    if (newestImageId <= lastReceivedImageId.current) return
    lastReceivedImageId.current = newestImageId
    if (session.isControlActive && !clipboardPanelOpen && !downloadPanelOpen) {
      setImageClipboardPanelOpen(true)
      setImageUnread(false)
    } else {
      setImageUnread(true)
    }
  }, [
    clipboardPanelOpen,
    downloadPanelOpen,
    newestImageId,
    session.isControlActive,
  ])

  useEffect(() => {
    if (videoElement.current) {
      videoElement.current.srcObject = session.mediaStream
    }
    if (!session.mediaStream) {
      setDisplayMode('fit')
      setElapsedSeconds(0)
      setFramesPerSecond(null)
      setVideoSize({ width: 0, height: 0 })
    }
  }, [session.mediaStream])

  useEffect(() => {
    if (!session.mediaStream) {
      return
    }
    const startedAt = Date.now()
    const updateElapsed = () => {
      setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1_000))
    }
    updateElapsed()
    const timer = window.setInterval(updateElapsed, 1_000)
    return () => window.clearInterval(timer)
  }, [session.mediaStream])

  useEffect(() => {
    const video = videoElement.current
    if (!session.mediaStream || !video || !video.requestVideoFrameCallback) {
      return
    }
    let frameCount = 0
    let measuredAt = performance.now()
    let callbackId = 0
    const onFrame: VideoFrameRequestCallback = (now) => {
      frameCount += 1
      const elapsed = now - measuredAt
      if (elapsed >= 1_000) {
        setFramesPerSecond(Math.round((frameCount * 1_000) / elapsed))
        frameCount = 0
        measuredAt = now
      }
      callbackId = video.requestVideoFrameCallback(onFrame)
    }
    callbackId = video.requestVideoFrameCallback(onFrame)
    return () => video.cancelVideoFrameCallback(callbackId)
  }, [session.mediaStream])

  useEffect(() => {
    const video = videoElement.current
    if (!video) {
      return
    }
    const updateVideoSize = () => {
      if (video.videoWidth > 0 && video.videoHeight > 0) {
        setVideoSize({ width: video.videoWidth, height: video.videoHeight })
      }
    }
    video.addEventListener('resize', updateVideoSize)
    return () => video.removeEventListener('resize', updateVideoSize)
  }, [])

  useEffect(() => {
    const onFullscreenChange = () => {
      const fullscreenActive = document.fullscreenElement === viewerStage.current
      setIsFullscreen(fullscreenActive)
      // Always start with an unobstructed remote view when entering fullscreen,
      // and discard stale drawer state when the browser exits it (including Esc).
      setFullscreenToolsOpen(false)
      setToolsPinned(false)
    }
    document.addEventListener('fullscreenchange', onFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange)
  }, [])

  useEffect(() => {
    if (!fullscreenToolsOpen) {
      setToolsBusy(false)
      setToolsHovered(false)
      setToolsKeyboardFocus(false)
    }
  }, [fullscreenToolsOpen])

  useEffect(() => {
    if (!isFullscreen || !fullscreenToolsOpen || toolsPinned || toolsHovered ||
        toolsKeyboardFocus || toolsBusy || disconnectConfirmOpen || !session.isControlActive) return
    const timer = window.setTimeout(() => setFullscreenToolsOpen(false), 4_000)
    return () => window.clearTimeout(timer)
  }, [isFullscreen, fullscreenToolsOpen, toolsPinned, toolsHovered,
    toolsKeyboardFocus, toolsBusy, disconnectConfirmOpen, session.isControlActive])

  useEffect(() => {
    if (!disconnectConfirmOpen) return
    const previousFocus = document.activeElement as HTMLElement | null
    cancelDisconnect.current?.focus()
    return () => previousFocus?.focus()
  }, [disconnectConfirmOpen])

  useEffect(() => {
    if (session.connectionState === 'offline') setDisconnectConfirmOpen(false)
  }, [session.connectionState])

  useInputCapture(videoElement, session.isControlActive && !disconnectConfirmOpen, {
    releaseRemoteInput: session.releaseRemoteInput,
    sendKey: session.sendKey,
    sendPointerButton: session.sendPointerButton,
    sendPointerMove: session.sendPointerMove,
    sendPointerWheel: session.sendPointerWheel,
  })

  const controlLabel = session.isControlActive
    ? '제어 활성 · 마우스/키보드 전달 중'
    : session.controlLocked
      ? '보기 전용 · 집 PC에서 긴급 잠금됨'
    : session.mediaStream
      ? session.controlPolicyEnabled
        ? '보기 전용 · 제어 권한 재연결 필요'
        : '보기 전용 (집 PC에서 제어를 허용하지 않음)'
      : null

  const quality = useMemo(
    () => getConnectionQuality(session.roundTripTimeMs, session.videoTelemetry),
    [session.roundTripTimeMs, session.videoTelemetry],
  )

  const videoStyle: CSSProperties | undefined =
    displayMode === 'actual' && videoSize.width > 0
      ? { width: videoSize.width, height: videoSize.height }
      : undefined

  const toggleTextClipboardPanel = () => {
    setClipboardPanelOpen((open) => !open)
    setImageClipboardPanelOpen(false)
    setDownloadPanelOpen(false)
  }

  const toggleImageClipboardPanel = () => {
    setImageClipboardPanelOpen((open) => !open)
    setImageUnread(false)
    setClipboardPanelOpen(false)
    setDownloadPanelOpen(false)
  }

  const toggleDownloadPanel = () => {
    setDownloadPanelOpen((open) => !open)
    setClipboardPanelOpen(false)
    setImageClipboardPanelOpen(false)
    session.requestFileList()
  }

  const toggleFullscreen = async () => {
    setPresentationError(null)
    try {
      if (document.fullscreenElement === viewerStage.current) {
        await document.exitFullscreen()
      } else {
        await viewerStage.current?.requestFullscreen()
      }
    } catch {
      setPresentationError('브라우저가 전체화면 전환을 허용하지 않았습니다.')
    }
  }

  const requestDisconnect = () => {
    session.releaseRemoteInput()
    setDisconnectConfirmOpen(true)
  }

  const toolbarProps = {
    videoProfile: session.videoProfile,
    videoProfilePending: session.videoProfilePending,
    videoProfileStabilizing: session.videoProfileStabilizing,
    videoProfileError: session.videoProfileError,
    canDisplay: Boolean(session.mediaStream) && videoSize.width > 0,
    hasVideo: Boolean(session.mediaStream),
    actualSize: displayMode === 'actual',
    isControlActive: session.isControlActive,
    canSendClipboardImage: session.canSendClipboardImage,
    canSendFiles: session.canSendFiles,
    textOpen: clipboardPanelOpen,
    imageOpen: imageClipboardPanelOpen,
    imageUnread,
    downloadsOpen: downloadPanelOpen,
    setVideoProfile: session.setVideoProfile,
    onDisplayMode: () => setDisplayMode((current) => current === 'fit' ? 'actual' : 'fit'),
    onFullscreen: () => void toggleFullscreen(),
    onCapture: () => sendCaptureShortcut(session.sendKey),
    onText: toggleTextClipboardPanel,
    onImage: toggleImageClipboardPanel,
    onUpload: () => fileInput.current?.click(),
    onDownload: toggleDownloadPanel,
  }

  return (
    <main className="app-shell">
      <header className="topbar" onPointerDownCapture={() => session.releaseRemoteInput()}>
        <div className="topbar-brand"><ViewerIcon name="monitor" /><h1>Mirror</h1></div>
        <span className="status-chip" data-state={session.connectionState}
          data-testid="connection-state" title={STATE_LABELS[session.connectionState]}>
          {STATE_LABELS[session.connectionState]}
        </span>
        <div className="topbar-menu"><ViewerToolbar {...toolbarProps} /></div>
        <div className="connection-action">
          {session.connectionState === 'offline' ? (
            <IconButton icon="connect" label={session.errorMessage && session.canRetry ? '다시 연결' : '원격 연결'}
              className="connect-button" data-testid="connect-button"
              disabled={!session.canConnect || Boolean(session.errorMessage && !session.canRetry)}
              onClick={session.connect} />
          ) : (
            <IconButton icon="disconnect" label="연결 종료" className="danger-button"
              data-testid="disconnect-button" onClick={requestDisconnect} />
          )}
        </div>
      </header>

      <section
        className="viewer-stage"
        aria-label="원격 화면"
        data-control-active={session.isControlActive}
        data-display-mode={displayMode}
        data-testid="viewer-stage"
        ref={viewerStage}
      >
        <video
          autoPlay
          className={session.mediaStream ? 'remote-video is-active' : 'remote-video'}
          data-testid="remote-video"
          muted
          onLoadedMetadata={(event) => {
            setVideoSize({
              width: event.currentTarget.videoWidth,
              height: event.currentTarget.videoHeight,
            })
          }}
          playsInline
          ref={videoElement}
          style={videoStyle}
          tabIndex={-1}
        />
        <input
          className="visually-hidden-input"
          data-testid="file-input"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) {
              session.sendFile(file)
            }
            event.target.value = ''
          }}
          ref={fileInput}
          type="file"
        />
        <FullscreenOverlayLayer active={isFullscreen} videoRef={videoElement}>
        {isFullscreen && (
          <>
            {!fullscreenToolsOpen && (
              <div className="fullscreen-tools-toggle"
                onPointerDown={(event) => { event.stopPropagation(); session.releaseRemoteInput() }}>
                <IconButton icon="tools" label="전체화면 도구 열기" data-testid="fullscreen-tools-toggle"
                  aria-controls="fullscreen-tools-drawer" aria-expanded={false}
                  onClick={() => setFullscreenToolsOpen(true)} />
              </div>
            )}
            {fullscreenToolsOpen && (
              <aside aria-label="전체화면 도구" className="fullscreen-tools-drawer"
                data-testid="fullscreen-tools-drawer" id="fullscreen-tools-drawer"
                onPointerEnter={() => setToolsHovered(true)} onPointerLeave={() => setToolsHovered(false)}
                onPointerDown={(event) => {
                  event.stopPropagation()
                  setToolsKeyboardFocus(false)
                  session.releaseRemoteInput()
                }}
                onKeyDown={(event) => { setToolsKeyboardFocus(true); event.stopPropagation() }}
                onKeyUp={(event) => event.stopPropagation()}
                onBlurCapture={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setToolsKeyboardFocus(false)
                }}>
                <header className="fullscreen-tools-header">
                  <IconButton icon="pin" label="도구함 고정" data-testid="fullscreen-tools-pin"
                    title={toolsPinned ? '고정 해제 · 자동 접힘' : '도구함 고정 · 자동 접힘 중지'}
                    aria-pressed={toolsPinned} onClick={() => setToolsPinned(!toolsPinned)} />
                  <IconButton icon="close" label="전체화면 도구 닫기" data-testid="fullscreen-tools-close"
                    onClick={() => setFullscreenToolsOpen(false)} />
                </header>
                <ViewerToolbar {...toolbarProps} fullscreen onBusyChange={setToolsBusy}
                  onCapture={() => { toolbarProps.onCapture(); setFullscreenToolsOpen(false) }}
                  onText={() => { toolbarProps.onText(); setFullscreenToolsOpen(false) }}
                  onImage={() => { toolbarProps.onImage(); setFullscreenToolsOpen(false) }}
                  onUpload={() => { toolbarProps.onUpload(); setFullscreenToolsOpen(false) }}
                  onDownload={() => { toolbarProps.onDownload(); setFullscreenToolsOpen(false) }} />
                {!session.isControlActive && (
                  <div className="fullscreen-tools-connection" data-testid="fullscreen-tools-connection" role="status">
                    <strong>제어 연결이 끊어졌습니다.</strong>
                    <span>메뉴 기능을 사용하려면 원격 연결을 다시 시작하세요.</span>
                    <IconButton icon="connect" label="다시 연결" className="connect-button" data-testid="fullscreen-reconnect-button"
                      disabled={!session.canConnect || Boolean(session.errorMessage && !session.canRetry)}
                      onClick={session.connect} />
                  </div>
                )}
                <div className="fullscreen-tools-footer">
                  <span className="palette-status" data-active={session.isControlActive}
                    title={controlLabel ?? STATE_LABELS[session.connectionState]}>
                    <span className="sr-only">{controlLabel ?? STATE_LABELS[session.connectionState]}</span>
                  </span>
                  {session.connectionState !== 'offline' && (
                    <IconButton icon="disconnect" label="연결 종료" className="danger-button"
                      data-testid="fullscreen-disconnect-button" onClick={requestDisconnect} />
                  )}
                </div>
              </aside>
            )}
          </>
        )}
        {!session.mediaStream && (
          <div className="empty-viewer">
            <strong>원격 연결 대기</strong>
            <p>
              집 PC 에이전트가 온라인이면 연결하여 주 모니터 화면을 표시합니다.
            </p>
          </div>
        )}
        {(session.fileTransfer || session.fileDownload) && (
          <div
            aria-label="파일 전송 상태"
            className="file-transfer-stack"
            data-testid="file-transfer-stack"
          >
            {session.fileTransfer && (
              <section
                className="file-transfer"
                data-status={session.fileTransfer.status}
                data-testid="file-transfer"
                role="status"
              >
                <header className="file-transfer-header">
                  <div>
                    <strong>파일 전송</strong>
                    <span>접속 PC → 원격 PC</span>
                  </div>
                  {(session.fileTransfer.status === 'done' ||
                    session.fileTransfer.status === 'error') && (
                    <button
                      aria-label="파일 전송 상태 닫기"
                      className="file-transfer-dismiss"
                      data-testid="file-transfer-dismiss"
                      onClick={session.clearFileTransfer}
                      type="button"
                    >
                      ×
                    </button>
                  )}
                </header>
                <div className="file-transfer-body">
                  <span className="file-transfer-name">
                    {session.fileTransfer.fileName}
                  </span>
                  <span className="file-transfer-status">
                    {session.fileTransfer.status === 'error'
                      ? `전송 실패 (${session.fileTransfer.errorCode ?? ''})`
                      : FILE_STATUS_LABEL[session.fileTransfer.status]}
                    {session.fileTransfer.status === 'sending' &&
                      ` ${Math.round(session.fileTransfer.progress * 100)}%`}
                  </span>
                </div>
                {(session.fileTransfer.status === 'sending' ||
                  session.fileTransfer.status === 'verifying') && (
                  <div
                    aria-label={`파일 전송 ${Math.round(
                      session.fileTransfer.progress * 100,
                    )}%`}
                    aria-valuemax={100}
                    aria-valuemin={0}
                    aria-valuenow={Math.round(
                      session.fileTransfer.progress * 100,
                    )}
                    className="file-transfer-track"
                    role="progressbar"
                  >
                    <div
                      className="file-transfer-fill"
                      style={{
                        width: `${Math.round(
                          session.fileTransfer.progress * 100,
                        )}%`,
                      }}
                    />
                  </div>
                )}
              </section>
            )}
            {session.fileDownload && (
              <section
                className="file-transfer"
                data-status={session.fileDownload.status}
                data-testid="file-download"
                role="status"
              >
                <header className="file-transfer-header">
                  <div>
                    <strong>파일 받기</strong>
                    <span>원격 PC → 접속 PC</span>
                  </div>
                  {(session.fileDownload.status === 'done' ||
                    session.fileDownload.status === 'error') && (
                    <button
                      aria-label="파일 받기 상태 닫기"
                      className="file-transfer-dismiss"
                      data-testid="file-download-dismiss"
                      onClick={session.clearFileDownload}
                      type="button"
                    >
                      ×
                    </button>
                  )}
                </header>
                <div className="file-transfer-body">
                  <span className="file-transfer-name">
                    {session.fileDownload.fileName}
                  </span>
                  <span className="file-transfer-status">
                    {session.fileDownload.status === 'error'
                      ? `받기 실패 (${session.fileDownload.errorCode ?? ''})`
                      : DOWNLOAD_STATUS_LABEL[session.fileDownload.status]}
                    {session.fileDownload.status === 'downloading' &&
                      ` ${Math.round(session.fileDownload.progress * 100)}%`}
                  </span>
                </div>
                {(session.fileDownload.status === 'downloading' ||
                  session.fileDownload.status === 'verifying') && (
                  <div
                    aria-label={`파일 받기 ${Math.round(
                      session.fileDownload.progress * 100,
                    )}%`}
                    aria-valuemax={100}
                    aria-valuemin={0}
                    aria-valuenow={Math.round(
                      session.fileDownload.progress * 100,
                    )}
                    className="file-transfer-track"
                    role="progressbar"
                  >
                    <div
                      className="file-transfer-fill"
                      style={{
                        width: `${Math.round(
                          session.fileDownload.progress * 100,
                        )}%`,
                      }}
                    />
                  </div>
                )}
              </section>
            )}
          </div>
        )}
        {downloadPanelOpen && (
          <aside
            className="download-panel"
            aria-label="집 PC에서 받을 파일"
            data-testid="download-panel"
          >
            <header className="download-panel-header">
              <div>
                <strong>파일 받기</strong>
                <span>원격 PC → 접속 PC</span>
              </div>
              <div className="download-panel-actions">
                <button
                  data-testid="download-refresh"
                  disabled={!session.canSendFiles}
                  onClick={() => session.requestFileList()}
                  type="button"
                >
                  새로고침
                </button>
                <button
                  aria-label="받기 닫기"
                  onClick={() => setDownloadPanelOpen(false)}
                  type="button"
                >
                  ×
                </button>
              </div>
            </header>
            <section className="download-panel-content">
              <h2>원격 PC의 Outgoing 폴더</h2>
              {session.downloadableFiles.length === 0 ? (
                <p className="download-empty">
                  받을 파일이 없습니다. 집 PC의 MirrorShare\Outgoing 폴더에 파일을
                  넣고 새로고침하세요.
                </p>
              ) : (
                <ul className="download-list">
                  {session.downloadableFiles.map((entry) => (
                    <li className="download-item" key={entry.name}>
                      <span className="download-name">{entry.name}</span>
                      <span className="download-size">{formatBytes(entry.size)}</span>
                      <button
                        data-testid={`download-file-${entry.name}`}
                        disabled={
                          session.fileDownload?.status === 'downloading' ||
                          session.fileDownload?.status === 'verifying'
                        }
                        onClick={() => session.downloadFile(entry.name)}
                        type="button"
                      >
                        받기
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </aside>
        )}
        {session.isControlActive && (
          <RemoteClipboard
            canSend={session.canSendClipboardText}
            open={clipboardPanelOpen}
            releaseRemoteInput={session.releaseRemoteInput}
            entries={session.clipboardEntries}
            dismissEntry={session.dismissClipboardEntry}
            copyText={copyTextToClipboard}
            onClose={() => setClipboardPanelOpen(false)}
            sendKey={session.sendKey}
            setRemoteClipboard={session.setRemoteClipboard}
          />
        )}
        {session.isControlActive && (
          <ImageClipboardPanel
            open={imageClipboardPanelOpen}
            releaseRemoteInput={session.releaseRemoteInput}
            canSend={session.canSendClipboardImage}
            dismissEntry={session.dismissClipboardImageEntry}
            entries={session.clipboardImageEntries}
            onClose={() => setImageClipboardPanelOpen(false)}
            sendKey={session.sendKey}
            sendRemoteImage={session.sendRemoteClipboardImage}
          />
        )}
        {session.clipboardEntries.length > 0 && !clipboardPanelOpen && !imageClipboardPanelOpen && (
          <aside
            className="clipboard-panel"
            aria-label="집 PC 클립보드"
            data-testid="clipboard-panel"
          >
            <header className="clipboard-panel-header">집 PC에서 복사한 내용</header>
            <ul className="clipboard-list">
              {session.clipboardEntries.map((entry) => (
                <li className="clipboard-item" key={entry.id}>
                  <p className="clipboard-text">{entry.text}</p>
                  <div className="clipboard-item-actions">
                    <button
                      data-testid={`clipboard-copy-${entry.id}`}
                      onClick={async () => {
                        const copied = await copyTextToClipboard(entry.text)
                        if (copied) {
                          setCopiedClipboardId(entry.id)
                        }
                      }}
                      type="button"
                    >
                      {copiedClipboardId === entry.id ? '복사됨' : '복사'}
                    </button>
                    <button
                      aria-label="삭제"
                      className="clipboard-dismiss"
                      onClick={() => session.dismissClipboardEntry(entry.id)}
                      type="button"
                    >
                      ×
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </aside>
        )}
        {disconnectConfirmOpen && (
          <div className="dialog-backdrop" onPointerDown={(event) => {
            event.stopPropagation()
            session.releaseRemoteInput()
          }}>
            <div ref={disconnectDialog} className="disconnect-dialog" role="dialog" aria-modal="true"
              aria-labelledby="disconnect-title" aria-describedby="disconnect-description"
              onKeyUp={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                event.stopPropagation()
                if (event.key === 'Escape') {
                  event.preventDefault()
                  setDisconnectConfirmOpen(false)
                }
                if (event.key === 'Tab') {
                  const buttons = disconnectDialog.current?.querySelectorAll<HTMLButtonElement>('button')
                  if (!buttons?.length) return
                  const first = buttons[0], last = buttons[buttons.length - 1]
                  if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault(); last?.focus()
                  } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault(); first?.focus()
                  }
                }
              }}>
              <h2 id="disconnect-title">원격 연결을 종료할까요?</h2>
              <p id="disconnect-description">집 PC는 계속 켜져 있으며, 이 브라우저의 원격 연결만 종료됩니다.</p>
              <div className="disconnect-dialog-actions">
                <button ref={cancelDisconnect} type="button" onClick={() => setDisconnectConfirmOpen(false)}>취소</button>
                <button className="confirm-disconnect" data-testid="confirm-disconnect-button" type="button"
                  onClick={() => { setDisconnectConfirmOpen(false); session.disconnect() }}>연결 종료</button>
              </div>
            </div>
          </div>
        )}

        </FullscreenOverlayLayer>
      </section>

      <section className="control-panel" aria-label="연결 제어">
        <div className="runtime-summary">
          {session.mediaStream && <>
            <span className="quality-indicator" data-tone={quality.tone}>{quality.label}</span>
            <span data-testid="selected-video-profile">{VIDEO_PROFILE_LABELS[session.videoProfile]} 선택</span>
            <span>{videoSize.width > 0 ? `${videoSize.width}×${videoSize.height}` : '해상도 측정 중'}</span>
            <span>{framesPerSecond === null ? 'FPS 측정 중' : `${framesPerSecond} FPS`}</span>
          </>}
          {controlLabel && <span className="control-badge" data-active={session.isControlActive}
            data-testid="control-status" title={controlLabel}>{controlLabel}</span>}
        </div>
        <details className="session-details">
          <summary><ViewerIcon name="stats" /><span>연결·영상 정보</span>
            <span data-testid="rtt-value">{session.roundTripTimeMs === null ? '측정 전' : `${session.roundTripTimeMs} ms`}</span>
          </summary>
          <div className="session-details-popover" onKeyDown={(event) => event.stopPropagation()}>
            <div className="connection-summary">
              <div><span className="control-label">대상 장치</span><strong>{session.deviceId ?? '개발 설정 없음'}</strong></div>
              <div><span className="control-label">DataChannel RTT</span><strong>{session.roundTripTimeMs === null ? '측정 전' : `${session.roundTripTimeMs} ms`}</strong></div>
            </div>
            {session.mediaStream && <div className="viewer-telemetry" aria-label="세션 품질 정보" data-testid="viewer-telemetry">
              <span data-testid="connection-route">
                {CONNECTION_ROUTE_LABELS[session.connectionRoute.route]}
                {session.connectionRoute.protocol
                  ? ` · ${session.connectionRoute.protocol.toUpperCase()}`
                  : ''}
              </span>
              <span>
                {videoSize.width > 0
                  ? `${videoSize.width}×${videoSize.height}`
                  : '해상도 측정 중'}
              </span>
              <span>
                {framesPerSecond === null ? 'FPS 측정 중' : `${framesPerSecond} FPS`}
              </span>
              {session.videoTelemetry.receivedBitrateMbps !== null && (
                <span data-testid="received-bitrate">
                  영상 사용량{' '}
                  {session.videoTelemetry.receivedBitrateMbps.toFixed(2)} Mbps
                </span>
              )}
              {session.videoTelemetry.packetLossPercent !== null && (
                <span data-testid="packet-loss">
                  손실 {session.videoTelemetry.packetLossPercent.toFixed(1)}%
                </span>
              )}
              {session.videoTelemetry.jitterMs !== null && (
                <span data-testid="video-jitter">
                  지터 {session.videoTelemetry.jitterMs.toFixed(1)} ms
                </span>
              )}
              {session.videoTelemetry.averageQp !== null && (
                <span data-testid="decoder-qp">
                  QP {session.videoTelemetry.averageQp.toFixed(1)}
                </span>
              )}
              <span>{formatSessionDuration(elapsedSeconds)}</span>
              {session.videoTelemetry.diagnostics && (
                <details data-testid="latency-diagnostics">
                  <summary>지연 상세</summary>
                  {session.videoTelemetry.timing && <ConnectionTimingDetails timing={session.videoTelemetry.timing} />}
                  <span>영상 버퍼 {session.videoTelemetry.diagnostics.bufferMs ?? '—'} ms · </span>
                  <span>프레임 디코딩 {session.videoTelemetry.diagnostics.decodeMs ?? '—'} ms · </span>
                  <span>최근 프레임 누락 {session.videoTelemetry.diagnostics.droppedFrames ?? '—'} · </span>
                  <span>입력 전송 대기 {(session.videoTelemetry.diagnostics.controlBufferedBytes / 1024).toFixed(1)} KiB · </span>
                  <span>세션 최대 {(session.videoTelemetry.diagnostics.controlPeakBytes / 1024).toFixed(1)} KiB</span>
                </details>
              )}
            </div>}
          </div>
        </details>
        {session.errorMessage && <div className="error-message" role="alert">
          <strong>{session.errorMessage}</strong>{session.errorAction && <span>{session.errorAction}</span>}
        </div>}
        {presentationError && <p className="error-message" role="alert">{presentationError}</p>}
      </section>

    </main>
  )
}
