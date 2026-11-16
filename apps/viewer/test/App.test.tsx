import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LatencyDiagnostics } from '../src/latencyMetrics'

const mocks = vi.hoisted(() => ({
  session: {
    canConnect: true,
    canSendClipboardText: true,
    canSendClipboardImage: false,
    canSendFiles: false,
    canRetry: false,
    clipboardEntries: [] as { id: number; receivedAt: number; text: string }[],
    clipboardImageEntries: [] as { blob: Blob; id: number; receivedAt: number }[],
    dismissClipboardEntry: vi.fn(),
    dismissClipboardImageEntry: vi.fn(),
    downloadableFiles: [] as { name: string; size: number }[],
    requestFileList: vi.fn(),
    downloadFile: vi.fn(),
    fileDownload: null as {
      errorCode: string | null
      fileName: string
      progress: number
      status: 'downloading' | 'verifying' | 'done' | 'error'
    } | null,
    clearFileDownload: vi.fn(),
    fileTransfer: null as {
      errorCode: string | null
      fileName: string
      progress: number
      status: 'preparing' | 'sending' | 'verifying' | 'done' | 'error'
    } | null,
    clearFileTransfer: vi.fn(),
    connect: vi.fn(),
    connectionState: 'offline' as const,
    controlGranted: false,
    controlLocked: false,
    controlPolicyEnabled: false,
    deviceId: 'device_test',
    disconnect: vi.fn(),
    errorAction: null as string | null,
    errorMessage: null as string | null,
    isControlActive: false,
    mediaStream: null as MediaStream | null,
    releaseRemoteInput: vi.fn(),
    roundTripTimeMs: null,
    connectionRoute: {
      protocol: null as string | null,
      route: 'unknown' as 'direct' | 'turn' | 'unknown',
    },
    videoTelemetry: {
      diagnostics: undefined as LatencyDiagnostics | undefined,
      averageQp: null as number | null,
      decodedFramesPerSecond: null as number | null,
      frameHeight: null as number | null,
      frameWidth: null as number | null,
      jitterMs: null as number | null,
      packetLossPercent: null as number | null,
      receivedBitrateMbps: null as number | null,
    },
    sendFile: vi.fn(),
    sendKey: vi.fn(),
    sendRemoteClipboardImage: vi.fn().mockResolvedValue(undefined),
    sendPointerButton: vi.fn(),
    sendPointerMove: vi.fn(),
    sendPointerWheel: vi.fn(),
    setRemoteClipboard: vi.fn().mockResolvedValue(undefined),
    setVideoProfile: vi.fn(),
    videoProfile: 'balanced' as const,
    videoProfileError: null as string | null,
    videoProfilePending: false,
    videoProfileStabilizing: false,
  },
}))

vi.mock('../src/useRemoteSession', () => ({
  useRemoteSession: () => mocks.session,
}))

import { App } from '../src/App'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  mocks.session.connectionState = 'offline'
  mocks.session.errorAction = null
  mocks.session.errorMessage = null
  mocks.session.canRetry = false
  mocks.session.isControlActive = false
  mocks.session.canSendClipboardText = true
  mocks.session.canSendClipboardImage = false
  mocks.session.canSendFiles = false
  mocks.session.clipboardImageEntries = []
  mocks.session.clipboardEntries = []
  mocks.session.downloadableFiles = []
  mocks.session.fileDownload = null
  mocks.session.fileTransfer = null
  mocks.session.mediaStream = null
  mocks.session.videoTelemetry.diagnostics = undefined
  mocks.session.videoProfilePending = false
  mocks.session.videoProfileStabilizing = false
  mocks.session.videoProfileError = null
  mocks.session.connectionRoute.protocol = null
  mocks.session.connectionRoute.route = 'unknown'
  mocks.session.videoTelemetry.averageQp = null
  mocks.session.videoTelemetry.decodedFramesPerSecond = null
  mocks.session.videoTelemetry.frameHeight = null
  mocks.session.videoTelemetry.frameWidth = null
  mocks.session.videoTelemetry.jitterMs = null
  mocks.session.videoTelemetry.packetLossPercent = null
  mocks.session.videoTelemetry.receivedBitrateMbps = null
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    value: null,
  })
})

function enterViewerFullscreen(): void {
  const stage = screen.getByTestId('viewer-stage')
  let fullscreenElement: Element | null = null
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    get: () => fullscreenElement,
  })
  Object.defineProperty(stage, 'requestFullscreen', {
    configurable: true,
    value: vi.fn(async () => {
      fullscreenElement = stage
      fireEvent(document, new Event('fullscreenchange'))
    }),
  })
  fireEvent.click(screen.getByTestId('fullscreen-button'))
}

function stubImageUrls() {
  vi.stubGlobal('URL', class extends URL {
    static override createObjectURL() { return 'blob:test-image' }
    static override revokeObjectURL() {}
  })
}

describe('App viewer controls', () => {
  it('shows actionable retry UX for a retryable failure', () => {
    mocks.session.errorMessage = '연결 서버에 도달할 수 없습니다.'
    mocks.session.errorAction = '네트워크를 확인하세요.'
    mocks.session.canRetry = true

    render(<App />)

    expect(screen.getByRole('alert').textContent).toContain('네트워크를 확인하세요.')
    fireEvent.click(screen.getByTestId('connect-button'))
    expect(screen.getByTestId('connect-button').textContent).toContain('다시 연결')
    expect(mocks.session.connect).toHaveBeenCalledOnce()
  })

  it('disables retry when the server marks the issue non-retryable', () => {
    mocks.session.errorMessage = '연결 권한이 만료되었습니다.'
    mocks.session.errorAction = '페이지를 새로 여세요.'
    mocks.session.canRetry = false

    render(<App />)

    expect((screen.getByTestId('connect-button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('uses the high-visibility style only for the connect action', () => {
    render(<App />)

    expect(screen.getByTestId('connect-button').classList.contains('connect-button')).toBe(
      true,
    )
    expect(screen.queryByTestId('disconnect-button')).toBeNull()
  })

  it('uses the danger interaction style only for the disconnect action', () => {
    mocks.session.connectionState = 'control-active' as never

    render(<App />)

    expect(screen.getByTestId('disconnect-button').classList.contains('danger-button')).toBe(
      true,
    )
    expect(screen.queryByTestId('connect-button')).toBeNull()
  })

  it('requests a live quality change from the toolbar', () => {
    render(<App />)

    fireEvent.click(screen.getByTestId('quality-button'))
    fireEvent.click(screen.getByTestId('video-profile-low'))

    expect(mocks.session.setVideoProfile).toHaveBeenCalledWith('low')
  })

  it('closes when the already-selected quality is clicked without requesting a redundant change', () => {
    render(<App />)
    fireEvent.click(screen.getByTestId('quality-button'))
    expect((screen.getByTestId('video-profile-balanced') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByTestId('video-profile-balanced'))
    expect(screen.getByTestId('quality-button').getAttribute('aria-expanded')).toBe('false')
    expect(mocks.session.setVideoProfile).not.toHaveBeenCalled()
  })

  it('shows a non-fatal quality timeout beside the selector', () => {
    mocks.session.videoProfileError = '화질 변경 확인이 지연되고 있습니다.'

    render(<App />)

    expect(screen.getByRole('alert').textContent).toContain('화질 변경 확인')
    expect((screen.getByTestId('video-profile-select') as HTMLSelectElement).disabled).toBe(false)
  })

  it('marks the viewer surface for a high-contrast control pointer', () => {
    mocks.session.isControlActive = true

    render(<App />)

    expect(screen.getByTestId('viewer-stage').dataset.controlActive).toBe('true')
  })

  it('does not render the removed keyboard, Hangul toggle, or hide menu controls', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true

    render(<App />)

    expect(screen.queryByTestId('keyboard-button')).toBeNull()
    expect(screen.queryByTestId('hangul-toggle-button')).toBeNull()
    expect(screen.queryByTestId('toolbar-hide-button')).toBeNull()
    expect(screen.queryByTestId('toolbar-show-button')).toBeNull()
    expect(screen.queryByTestId('mobile-keyboard-input')).toBeNull()
  })

  it('offers a high video profile option and requests it on selection', () => {
    render(<App />)

    fireEvent.click(screen.getByTestId('quality-button'))
    const select = screen.getByTestId('video-profile-select') as HTMLFieldSetElement
    const options = Array.from(select.querySelectorAll('input')).map((option) => option.value)
    expect(options).toContain('high')
    expect(screen.getByTestId('video-profile-high').closest('label')?.textContent).toContain(
      '1920×1080 고정 / 20fps',
    )
    expect(screen.getByTestId('video-profile-balanced').closest('label')?.textContent).toContain(
      '1600×900 고정 / 15fps',
    )
    expect(screen.getByTestId('video-profile-low').closest('label')?.textContent).toContain(
      '1280×720 고정 / 10fps',
    )

    fireEvent.click(screen.getByTestId('video-profile-high'))

    expect(mocks.session.setVideoProfile).toHaveBeenCalledWith('high')
  })

  it('shows received bitrate, loss, jitter, and decoder quality telemetry', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.videoTelemetry.receivedBitrateMbps = 0.42
    mocks.session.videoTelemetry.packetLossPercent = 5.3
    mocks.session.videoTelemetry.jitterMs = 37.5
    mocks.session.videoTelemetry.averageQp = 41.2
    mocks.session.connectionRoute.protocol = 'udp'
    mocks.session.connectionRoute.route = 'turn'

    render(<App />)

    expect(screen.getByTestId('received-bitrate').textContent).toContain('0.42 Mbps')
    expect(screen.getByTestId('received-bitrate').textContent).toContain('영상 사용량')
    expect(screen.getByTestId('selected-video-profile').textContent).toContain(
      'Balanced',
    )
    expect(screen.getByTestId('connection-route').textContent).toContain(
      'TURN 중계 · UDP',
    )
    expect(screen.getByTestId('packet-loss').textContent).toContain('5.3%')
    expect(screen.getByTestId('video-jitter').textContent).toContain('37.5 ms')
    expect(screen.getByTestId('decoder-qp').textContent).toContain('41.2')
  })

  it('shows optional latency details with missing measurements distinguished from zero', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.videoTelemetry.diagnostics = {
      bufferMs: 50, decodeMs: null, droppedFrames: 0, controlBufferedBytes: 1024, controlPeakBytes: 5120,
    }
    render(<App />)
    const details = screen.getByTestId('latency-diagnostics')
    expect(details.textContent).toContain('영상 버퍼 50 ms')
    expect(details.textContent).toContain('프레임 디코딩 — ms')
    expect(details.textContent).toContain('최근 프레임 누락 0')
    expect(details.textContent).toContain('입력 전송 대기 1.0 KiB')
    expect(details.textContent).toContain('세션 최대 5.0 KiB')
    expect(details.hasAttribute('open')).toBe(false)
  })

  it('blocks repeated quality changes while the decoded video stabilizes', () => {
    mocks.session.videoProfileStabilizing = true

    render(<App />)

    expect(
      (screen.getByTestId('video-profile-select') as HTMLSelectElement).disabled,
    ).toBe(true)
    expect(screen.getByRole('status').textContent).toContain('영상 안정화 중')
  })

  it('sends the Win+Shift+S capture chord in down/reverse-up order when control is active', () => {
    mocks.session.isControlActive = true

    render(<App />)
    fireEvent.click(screen.getByTestId('capture-button'))

    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(1, 'MetaLeft', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(2, 'ShiftLeft', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(3, 'KeyS', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(4, 'KeyS', 'up')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(5, 'ShiftLeft', 'up')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(6, 'MetaLeft', 'up')
    expect(mocks.session.sendKey).toHaveBeenCalledTimes(6)
  })

  it('disables the capture button until control is active', () => {
    mocks.session.isControlActive = false

    render(<App />)

    expect((screen.getByTestId('capture-button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('writes the remote clipboard and sends a Ctrl+V chord from the clipboard panel', async () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true

    render(<App />)
    expect(screen.queryByTestId('remote-clipboard-input')).toBeNull()

    fireEvent.click(screen.getByTestId('clipboard-button'))
    const textClipboard = screen.getByTestId('remote-clipboard')
    expect(textClipboard.tagName).toBe('ASIDE')
    expect(textClipboard.textContent).toContain('텍스트 클립보드')
    expect(textClipboard.textContent).toContain('접속 PC → 원격 PC')
    expect(screen.getByTestId('remote-clipboard-paste').textContent).toContain(
      '원격 PC에 붙여넣기',
    )
    fireEvent.change(screen.getByTestId('remote-clipboard-input'), {
      target: { value: '회사에서 복사한 텍스트' },
    })
    await act(async () => { fireEvent.click(screen.getByTestId('remote-clipboard-paste')) })

    expect(mocks.session.setRemoteClipboard).toHaveBeenCalledWith('회사에서 복사한 텍스트', expect.any(Function))
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(1, 'ControlLeft', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(2, 'KeyV', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(3, 'KeyV', 'up')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(4, 'ControlLeft', 'up')
    expect(mocks.session.sendKey).toHaveBeenCalledTimes(4)
  })

  it('keeps the clipboard paste action disabled until text is entered', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true

    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))

    expect((screen.getByTestId('remote-clipboard-paste') as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('retains 85,000 characters and waits for applied before one paste, even across panel switching', async () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    let applied!: () => void
    mocks.session.setRemoteClipboard.mockImplementationOnce(() => new Promise<void>(resolve => { applied = resolve }))
    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    const input = screen.getByTestId('remote-clipboard-input') as HTMLTextAreaElement
    expect(input.maxLength).toBe(1_048_576)
    const text = '한'.repeat(85_000)
    fireEvent.change(input, { target: { value: text } })
    fireEvent.click(screen.getByTestId('remote-clipboard-paste'))
    fireEvent.click(screen.getByTestId('remote-clipboard-paste'))
    expect(mocks.session.setRemoteClipboard).toHaveBeenCalledTimes(1)
    expect(mocks.session.setRemoteClipboard).toHaveBeenCalledWith(text, expect.any(Function))
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('remote-clipboard-close'))
    fireEvent.click(screen.getByTestId('clipboard-button'))
    expect((screen.getByTestId('remote-clipboard-input') as HTMLTextAreaElement).value).toBe(text)
    expect((screen.getByTestId('remote-clipboard-paste') as HTMLButtonElement).disabled).toBe(true)
    await act(async () => { applied() })
    expect(mocks.session.sendKey).toHaveBeenCalledTimes(4)
    expect(screen.getByTestId('remote-clipboard-status').textContent).toContain('85,000자')
  })

  it('does not paste after transfer rejection and preserves the draft', async () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    mocks.session.setRemoteClipboard.mockRejectedValueOnce(new Error('TIMEOUT'))
    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.change(screen.getByTestId('remote-clipboard-input'), { target: { value: '실패해도 유지' } })
    await act(async () => { fireEvent.click(screen.getByTestId('remote-clipboard-paste')) })
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
    expect(screen.getByTestId('remote-clipboard-status').getAttribute('data-tone')).toBe('error')
    expect((screen.getByTestId('remote-clipboard-input') as HTMLTextAreaElement).value).toBe('실패해도 유지')
  })

  it('accepts 262,144 characters without truncation and reports the same cap for overflow', async () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    const input = screen.getByTestId('remote-clipboard-input')
    fireEvent.change(input, { target: { value: '한'.repeat(1_048_577) } })
    fireEvent.click(screen.getByTestId('remote-clipboard-paste'))
    expect(screen.getByTestId('remote-clipboard-status').textContent).toContain('1,048,576자를 초과')
    expect(mocks.session.setRemoteClipboard).not.toHaveBeenCalled()
    const text = '한'.repeat(1_048_576)
    fireEvent.change(input, { target: { value: text } })
    await act(async () => { fireEvent.click(screen.getByTestId('remote-clipboard-paste')) })
    expect(mocks.session.setRemoteClipboard).toHaveBeenCalledWith(text, expect.any(Function))
    expect(mocks.session.sendKey).toHaveBeenCalledTimes(4)
    expect(screen.getByTestId('remote-clipboard-status').textContent).toContain('1,048,576자')
  })

  it('does not issue old paste keys after the control session unmounts', async () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    let applied!: () => void
    mocks.session.setRemoteClipboard.mockImplementationOnce(() => new Promise<void>(resolve => { applied = resolve }))
    const { unmount } = render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.change(screen.getByTestId('remote-clipboard-input'), { target: { value: 'old session' } })
    fireEvent.click(screen.getByTestId('remote-clipboard-paste'))
    unmount()
    await act(async () => { applied() })
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
  })

  it('requires an open text channel before allowing a paste', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardText = false
    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.change(screen.getByTestId('remote-clipboard-input'), { target: { value: 'waiting' } })
    expect((screen.getByTestId('remote-clipboard-paste') as HTMLButtonElement).disabled).toBe(true)
    expect(mocks.session.setRemoteClipboard).not.toHaveBeenCalled()
  })

  it('disables the clipboard button until control is active', () => {
    mocks.session.isControlActive = false

    render(<App />)

    expect((screen.getByTestId('clipboard-button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('opens the image clipboard only when control and its channel are ready', () => {
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true

    render(<App />)
    fireEvent.click(screen.getByTestId('image-clipboard-button'))

    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
    expect(screen.getByTestId('image-clipboard-button').textContent).toContain(
      '이미지 클립보드',
    )
    expect((screen.getByTestId('image-paste-remote') as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('keeps the text and image clipboard panels mutually exclusive', () => {
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true

    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    expect(screen.getByTestId('remote-clipboard')).not.toBeNull()

    fireEvent.click(screen.getByTestId('image-clipboard-button'))

    expect(screen.queryByTestId('remote-clipboard')).toBeNull()
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
  })

  it('keeps the file receive and clipboard panels mutually exclusive', () => {
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    mocks.session.canSendFiles = true

    render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    expect(screen.getByTestId('remote-clipboard')).not.toBeNull()

    fireEvent.click(screen.getByTestId('file-receive-button'))
    expect(screen.queryByTestId('remote-clipboard')).toBeNull()
    expect(screen.getByTestId('download-panel')).not.toBeNull()

    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    expect(screen.queryByTestId('download-panel')).toBeNull()
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
  })

  it('disables image clipboard transfer while its dedicated channel is unavailable', () => {
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = false

    render(<App />)

    expect(
      (screen.getByTestId('image-clipboard-button') as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('uses explicit toolbar labels for text, image, and file receiving', () => {
    render(<App />)

    expect(screen.getByTestId('clipboard-button').textContent).toContain(
      '텍스트 클립보드',
    )
    expect(screen.getByTestId('image-clipboard-button').textContent).toContain(
      '이미지 클립보드',
    )
    expect(screen.getByTestId('file-receive-button').textContent).toContain(
      '파일 받기',
    )
    expect(screen.getByTestId('clipboard-button').getAttribute('title')).toBe(
      '접속 PC와 원격 PC 사이에서 텍스트를 복사하고 붙여넣기',
    )
    expect(screen.getByTestId('image-clipboard-button').getAttribute('title')).toBe(
      '접속 PC와 원격 PC 사이에서 이미지를 복사하고 붙여넣기',
    )
  })

  it('uses clipboard-style cards for file sending, receiving, and the file list', () => {
    mocks.session.canSendFiles = true
    mocks.session.downloadableFiles = [
      { name: 'project-report.pdf', size: 2_621_440 },
    ]
    mocks.session.fileTransfer = {
      errorCode: null,
      fileName: 'design-data.zip',
      progress: 0.42,
      status: 'sending',
    }
    mocks.session.fileDownload = {
      errorCode: null,
      fileName: 'reference-image.png',
      progress: 0.64,
      status: 'downloading',
    }

    const view = render(<App />)
    fireEvent.click(screen.getByTestId('file-receive-button'))

    const sendCard = screen.getByTestId('file-transfer')
    expect(sendCard.tagName).toBe('SECTION')
    expect(sendCard.textContent).toContain('파일 전송')
    expect(sendCard.textContent).toContain('접속 PC → 원격 PC')
    expect(sendCard.textContent).toContain('design-data.zip')
    expect(sendCard.textContent).toContain('42%')

    const receiveCard = screen.getByTestId('file-download')
    expect(receiveCard.tagName).toBe('SECTION')
    expect(receiveCard.textContent).toContain('파일 받기')
    expect(receiveCard.textContent).toContain('원격 PC → 접속 PC')
    expect(receiveCard.textContent).toContain('reference-image.png')
    expect(receiveCard.textContent).toContain('64%')

    const progressLabels = screen
      .getAllByRole('progressbar')
      .map((progress) => progress.getAttribute('aria-label'))
    expect(progressLabels).toEqual(['파일 전송 42%', '파일 받기 64%'])

    const receivePanel = screen.getByTestId('download-panel')
    expect(receivePanel.tagName).toBe('ASIDE')
    expect(receivePanel.textContent).toContain('파일 받기')
    expect(receivePanel.textContent).toContain('원격 PC → 접속 PC')
    expect(receivePanel.textContent).toContain('원격 PC의 Outgoing 폴더')
    expect(receivePanel.textContent).toContain('project-report.pdf')
    expect(receivePanel.textContent).toContain('2.5 MB')

    expect(
      (screen.getByTestId(
        'download-file-project-report.pdf',
      ) as HTMLButtonElement).disabled,
    ).toBe(true)

    mocks.session.fileDownload = null
    view.rerender(<App />)
    fireEvent.click(screen.getByTestId('download-file-project-report.pdf'))
    expect(mocks.session.downloadFile).toHaveBeenCalledWith('project-report.pdf')
  })

  it('keeps completed file status cards dismissible', () => {
    mocks.session.fileTransfer = {
      errorCode: null,
      fileName: 'sent.txt',
      progress: 1,
      status: 'done',
    }
    mocks.session.fileDownload = {
      errorCode: null,
      fileName: 'received.txt',
      progress: 1,
      status: 'done',
    }

    render(<App />)
    fireEvent.click(screen.getByTestId('file-transfer-dismiss'))
    fireEvent.click(screen.getByTestId('file-download-dismiss'))

    expect(mocks.session.clearFileTransfer).toHaveBeenCalledOnce()
    expect(mocks.session.clearFileDownload).toHaveBeenCalledOnce()
  })

  it('shows the requested tools in a collapsed drawer only while fullscreen', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    mocks.session.canSendFiles = true

    render(<App />)
    expect(screen.queryByTestId('fullscreen-tools-toggle')).toBeNull()

    enterViewerFullscreen()

    expect(screen.getByTestId('fullscreen-tools-toggle').textContent).toContain(
      '도구 열기',
    )
    expect(screen.queryByTestId('fullscreen-tools-drawer')).toBeNull()

    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))

    const drawer = screen.getByTestId('fullscreen-tools-drawer')
    expect(drawer.textContent).toContain('화질')
    expect(drawer.textContent).toContain('캡처')
    expect(drawer.textContent).toContain('텍스트 클립보드')
    expect(drawer.textContent).toContain('이미지 클립보드')
    expect(drawer.textContent).toContain('파일 전송')
    expect(drawer.textContent).toContain('파일 받기')
  })

  it('keeps fullscreen tool pointer input local and releases remote held input', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true

    render(<App />)
    enterViewerFullscreen()

    fireEvent.pointerDown(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.pointerDown(screen.getByTestId('fullscreen-tools-drawer'))

    expect(mocks.session.releaseRemoteInput).toHaveBeenCalledTimes(2)
    expect(mocks.session.sendPointerButton).not.toHaveBeenCalled()
    expect(screen.getByTestId('fullscreen-tools-drawer')).not.toBeNull()
  })

  it('offers reconnect inside fullscreen when the control session drops', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true

    const view = render(<App />)
    enterViewerFullscreen()
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))

    mocks.session.connectionState = 'offline'
    mocks.session.errorMessage =
      '다른 기기·탭에서 접속하여 이 세션이 종료되었습니다.'
    mocks.session.canRetry = true
    mocks.session.isControlActive = false
    mocks.session.mediaStream = null
    view.rerender(<App />)

    expect(screen.getByTestId('fullscreen-tools-connection').textContent).toContain(
      '제어 연결이 끊어졌습니다.',
    )
    fireEvent.click(screen.getByTestId('fullscreen-reconnect-button'))
    expect(mocks.session.connect).toHaveBeenCalledOnce()
    expect(document.fullscreenElement).toBe(screen.getByTestId('viewer-stage'))
  })

  it('reuses quality and capture actions from the fullscreen drawer', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true

    render(<App />)
    enterViewerFullscreen()
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-quality-button'))

    expect(
      screen.getByTestId('fullscreen-video-profile-high').closest('label')?.textContent,
    ).toContain('1920×1080 고정 / 20fps')
    expect(
      screen.getByTestId('fullscreen-video-profile-balanced').closest('label')?.textContent,
    ).toContain('1600×900 고정 / 15fps')
    expect(
      screen.getByTestId('fullscreen-video-profile-low').closest('label')?.textContent,
    ).toContain('1280×720 고정 / 10fps')

    fireEvent.click(screen.getByTestId('fullscreen-video-profile-high'))
    expect(mocks.session.setVideoProfile).toHaveBeenCalledWith('high')

    fireEvent.click(screen.getByTestId('fullscreen-capture-button'))
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(1, 'MetaLeft', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(6, 'MetaLeft', 'up')
    expect(screen.queryByTestId('fullscreen-tools-drawer')).toBeNull()
  })

  it('preserves text drafts when closing or switching panels, but clears them when control ends', () => {
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    const view = render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.change(screen.getByTestId('remote-clipboard-input'), { target: { value: '작성 중인 초안' } })
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    fireEvent.click(screen.getByTestId('clipboard-button'))
    expect((screen.getByTestId('remote-clipboard-input') as HTMLTextAreaElement).value).toBe('작성 중인 초안')
    fireEvent.click(screen.getByTestId('remote-clipboard-close'))
    fireEvent.click(screen.getByTestId('clipboard-button'))
    expect((screen.getByTestId('remote-clipboard-input') as HTMLTextAreaElement).value).toBe('작성 중인 초안')
    mocks.session.isControlActive = false
    view.rerender(<App />)
    mocks.session.isControlActive = true
    view.rerender(<App />)
    expect((screen.getByTestId('remote-clipboard-input') as HTMLTextAreaElement).value).toBe('')
  })

  it('keeps image-paste keys local and closes on Escape with focus restored', () => {
    mocks.session.isControlActive = true
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.canSendClipboardImage = true
    render(<App />)
    const trigger = screen.getByTestId('image-clipboard-button')
    trigger.focus()
    fireEvent.click(trigger)
    const target = screen.getByTestId('image-paste-target')
    expect(document.activeElement).toBe(target)
    expect(fireEvent.keyDown(target, { key: 'v', code: 'KeyV', ctrlKey: true })).toBe(true)
    fireEvent.keyUp(target, { key: 'v', code: 'KeyV', ctrlKey: true })
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
    fireEvent.keyDown(target, { key: 'Escape', code: 'Escape' })
    expect(screen.queryByTestId('image-clipboard-panel')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
  })

  it('only auto-opens new received images and detects arrivals in a full history', () => {
    stubImageUrls()
    const entry = (id: number) => ({ id, receivedAt: Date.now(), blob: new Blob(['png'], { type: 'image/png' }) })
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    mocks.session.clipboardImageEntries = [6, 5, 4, 3, 2, 1].map(entry)
    const view = render(<App />)
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '이미지 클립보드 닫기' }))
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.click(screen.getByTestId('remote-clipboard-close'))
    expect(screen.queryByTestId('image-clipboard-panel')).toBeNull()
    mocks.session.clipboardImageEntries = [7, 6, 5, 4, 3, 2].map(entry)
    view.rerender(<App />)
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '이미지 클립보드 닫기' }))
    mocks.session.clipboardImageEntries = [6, 5, 4, 3, 2].map(entry)
    view.rerender(<App />)
    expect(screen.queryByTestId('image-clipboard-panel')).toBeNull()
  })

  it('marks images arriving during text editing without stealing focus or reopening old images', () => {
    stubImageUrls()
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    const view = render(<App />)
    fireEvent.click(screen.getByTestId('clipboard-button'))
    mocks.session.clipboardImageEntries = [{ id: 1, receivedAt: Date.now(), blob: new Blob(['png'], { type: 'image/png' }) }]
    view.rerender(<App />)
    expect(screen.queryByTestId('image-clipboard-panel')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('remote-clipboard-input'))
    expect(screen.getByTestId('image-clipboard-button').dataset.unread).toBe('true')
    fireEvent.click(screen.getByTestId('remote-clipboard-close'))
    expect(screen.queryByTestId('image-clipboard-panel')).toBeNull()
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
    expect(screen.getByTestId('image-clipboard-button').dataset.unread).toBeUndefined()
  })

  it('retains selected images and in-flight state across panel switching and pastes only after acknowledgement', async () => {
    stubImageUrls()
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    let complete!: () => void
    mocks.session.sendRemoteClipboardImage.mockImplementationOnce(() => new Promise<void>(resolve => { complete = resolve }))
    render(<App />)
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    const image = new Blob(['png'], { type: 'image/png' })
    await act(async () => {
      fireEvent.paste(screen.getByTestId('image-paste-target'), { clipboardData: { items: [{ type: 'image/png', getAsFile: () => image }] } })
    })
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    expect((screen.getByTestId('image-paste-remote') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByTestId('image-paste-remote'))
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('clipboard-button'))
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    expect((screen.getByTestId('image-paste-remote') as HTMLButtonElement).disabled).toBe(true)
    expect(mocks.session.sendRemoteClipboardImage).toHaveBeenCalledOnce()
    await act(async () => { complete() })
    expect(mocks.session.sendKey).toHaveBeenCalledTimes(4)
    expect(screen.getByRole('status').textContent).toContain('붙여넣기 요청')
  })

  it('shows clipboard permission failures distinctly from success', async () => {
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    render(<App />)
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    // jsdom has no clipboard.read, so this follows the unsupported-browser path.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '이미지 불러오기' })) })
    expect(screen.getByRole('status').textContent).toContain('클립보드 접근을 지원하지')
    expect(screen.getByRole('status').getAttribute('data-tone')).toBe('error')
  })

  it('shows incoming text history inside its panel instead of an overlapping second panel', () => {
    mocks.session.isControlActive = true
    mocks.session.clipboardEntries = [{ id: 1, receivedAt: Date.now(), text: '원격에서 복사한 내용' }]
    render(<App />)
    expect(screen.getByTestId('clipboard-panel')).not.toBeNull()
    fireEvent.click(screen.getByTestId('clipboard-button'))
    expect(screen.queryByTestId('clipboard-panel')).toBeNull()
    expect(screen.getByTestId('remote-clipboard').textContent).toContain('원격에서 복사한 내용')
  })

  it('requires confirmation to disconnect and restores focus on cancellation', () => {
    mocks.session.connectionState = 'control-active' as never
    render(<App />)
    const disconnect = screen.getByTestId('disconnect-button')
    disconnect.focus()
    fireEvent.click(disconnect)
    expect(mocks.session.disconnect).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog').textContent).toContain('집 PC는 계속 켜져')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '취소' }))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape', code: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(disconnect)
    expect(mocks.session.disconnect).not.toHaveBeenCalled()
    fireEvent.click(disconnect)
    fireEvent.click(screen.getByTestId('confirm-disconnect-button'))
    expect(mocks.session.disconnect).toHaveBeenCalledOnce()
  })

  it('auto-collapses after inactivity but holds while hovered or pinned', () => {
    vi.useFakeTimers()
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    render(<App />)
    enterViewerFullscreen()
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.pointerEnter(screen.getByTestId('fullscreen-tools-drawer'))
    fireEvent.pointerLeave(screen.getByTestId('fullscreen-tools-drawer'))
    act(() => vi.advanceTimersByTime(4_001))
    expect(screen.queryByTestId('fullscreen-tools-drawer')).toBeNull()
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.pointerEnter(screen.getByTestId('fullscreen-tools-drawer'))
    act(() => vi.advanceTimersByTime(6_000))
    expect(screen.getByTestId('fullscreen-tools-drawer')).not.toBeNull()
    fireEvent.pointerLeave(screen.getByTestId('fullscreen-tools-drawer'))
    fireEvent.click(screen.getByTestId('fullscreen-tools-pin'))
    act(() => vi.advanceTimersByTime(6_000))
    expect(screen.getByTestId('fullscreen-tools-pin').getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByTestId('fullscreen-tools-pin'))
    act(() => vi.advanceTimersByTime(4_001))
    expect(screen.queryByTestId('fullscreen-tools-drawer')).toBeNull()
  })

  it('does not collapse during quality selection or keyboard interaction', () => {
    vi.useFakeTimers()
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    render(<App />)
    enterViewerFullscreen()
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-quality-button'))
    act(() => vi.advanceTimersByTime(6_000))
    expect(screen.getByTestId('fullscreen-tools-drawer')).not.toBeNull()
    fireEvent.keyDown(screen.getByTestId('fullscreen-quality-button'), { key: 'Tab', code: 'Tab' })
    expect(mocks.session.sendKey).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(6_000))
    expect(screen.getByTestId('fullscreen-tools-drawer')).not.toBeNull()
  })

  it('keeps top-menu quick actions grouped separately from file transfers', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    mocks.session.canSendFiles = true
    render(<App />)

    const assertGroups = (prefix: string) => {
      const quickActions = screen.getByTestId(`${prefix}capture-button`).closest('.toolbar-group')
      expect(quickActions?.classList.contains('capture-actions')).toBe(true)
      expect(screen.getByTestId(`${prefix}clipboard-button`).closest('.toolbar-group')).toBe(quickActions)
      expect(screen.getByTestId(`${prefix}image-clipboard-button`).closest('.toolbar-group')).toBe(quickActions)
      const fileActions = screen.getByTestId(`${prefix}file-send-button`).closest('.toolbar-group')
      expect(fileActions).not.toBe(quickActions)
      expect(fileActions?.classList.contains('secondary-actions')).toBe(true)
      expect(fileActions?.querySelectorAll('button').length).toBe(2)
      expect(screen.getByTestId(`${prefix}file-receive-button`).closest('.toolbar-group')).toBe(fileActions)
    }

    assertGroups('')
    fireEvent.click(screen.getByTestId('toolbar-more-button'))
    fireEvent.click(screen.getByTestId('image-clipboard-button'))
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
    expect(screen.getByTestId('toolbar-more-button').getAttribute('aria-expanded')).toBe('false')
  })

  it('orders fullscreen display tools on the left and clipboard/file tools on the right', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    mocks.session.canSendFiles = true
    render(<App />)
    enterViewerFullscreen()
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    const toolbar = screen.getByRole('toolbar', {name: '전체화면 메뉴'})
    const ids = (group: string) => Array.from(toolbar.querySelectorAll(`${group} button[data-testid]`))
      .map(button => button.getAttribute('data-testid'))
    expect(ids('.display-actions')).toEqual([
      'fullscreen-quality-button', 'fullscreen-display-mode-button',
      'fullscreen-exit-button', 'fullscreen-capture-button',
    ])
    expect(ids('.capture-actions')).toEqual(['fullscreen-clipboard-button', 'fullscreen-image-clipboard-button'])
    expect(ids('.secondary-actions')).toEqual(['fullscreen-file-send-button', 'fullscreen-file-receive-button'])
    expect(toolbar.querySelectorAll('[data-testid="fullscreen-capture-button"]')).toHaveLength(1)
    expect(screen.getByTestId('fullscreen-tools-close')).toBeDefined()
  })

  it('uses an icon capture button and unambiguous file direction tooltips', () => {
    render(<App />)
    expect(screen.getByTestId('capture-button').querySelector('svg')).not.toBeNull()
    expect(screen.getByTestId('capture-button').title).toContain('원격 PC 화면 캡처')
    expect(screen.getByTestId('file-send-button').title).toBe('집 PC로 파일 보내기')
    expect(screen.getByTestId('file-receive-button').title).toBe('집 PC에서 파일 받기')
  })

  it('opens fullscreen clipboard and file tools without leaving fullscreen', () => {
    mocks.session.mediaStream = {} as MediaStream
    mocks.session.isControlActive = true
    mocks.session.canSendClipboardImage = true
    mocks.session.canSendFiles = true

    render(<App />)
    enterViewerFullscreen()

    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-clipboard-button'))
    expect(screen.getByTestId('remote-clipboard')).not.toBeNull()
    expect(document.fullscreenElement).toBe(screen.getByTestId('viewer-stage'))

    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-image-clipboard-button'))
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()

    const fileInput = screen.getByTestId('file-input') as HTMLInputElement
    expect(screen.getByTestId('viewer-stage').contains(fileInput)).toBe(true)
    const filePicker = vi.spyOn(fileInput, 'click')
    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-file-send-button'))
    expect(filePicker).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByTestId('fullscreen-tools-toggle'))
    fireEvent.click(screen.getByTestId('fullscreen-file-receive-button'))
    expect(mocks.session.requestFileList).toHaveBeenCalledOnce()
    expect(screen.getByTestId('download-panel')).not.toBeNull()
    expect(document.fullscreenElement).toBe(screen.getByTestId('viewer-stage'))
  })
})
