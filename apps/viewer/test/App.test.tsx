import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  session: {
    canConnect: true,
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
    setRemoteClipboard: vi.fn(),
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
  vi.clearAllMocks()
  mocks.session.connectionState = 'offline'
  mocks.session.errorAction = null
  mocks.session.errorMessage = null
  mocks.session.canRetry = false
  mocks.session.isControlActive = false
  mocks.session.canSendClipboardImage = false
  mocks.session.canSendFiles = false
  mocks.session.clipboardImageEntries = []
  mocks.session.downloadableFiles = []
  mocks.session.fileDownload = null
  mocks.session.fileTransfer = null
  mocks.session.mediaStream = null
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

    fireEvent.change(screen.getByTestId('video-profile-select'), {
      target: { value: 'low' },
    })

    expect(mocks.session.setVideoProfile).toHaveBeenCalledWith('low')
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

    const select = screen.getByTestId('video-profile-select') as HTMLSelectElement
    const options = Array.from(select.options).map((option) => option.value)
    expect(options).toContain('high')
    expect(select.querySelector('option[value="high"]')?.textContent).toContain(
      '1920×1080 고정 / 20fps',
    )
    expect(select.querySelector('option[value="balanced"]')?.textContent).toContain(
      '1600×900 고정 / 15fps',
    )
    expect(select.querySelector('option[value="low"]')?.textContent).toContain(
      '1280×720 고정 / 10fps',
    )

    fireEvent.change(select, { target: { value: 'high' } })

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

  it('writes the remote clipboard and sends a Ctrl+V chord from the clipboard panel', () => {
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
    fireEvent.click(screen.getByTestId('remote-clipboard-paste'))

    expect(mocks.session.setRemoteClipboard).toHaveBeenCalledWith('회사에서 복사한 텍스트')
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

    const fullscreenSelect = screen.getByTestId(
      'fullscreen-video-profile-select',
    ) as HTMLSelectElement
    expect(
      fullscreenSelect.querySelector('option[value="high"]')?.textContent,
    ).toContain('1920×1080 고정 / 20fps')
    expect(
      fullscreenSelect.querySelector('option[value="balanced"]')?.textContent,
    ).toContain('1600×900 고정 / 15fps')
    expect(
      fullscreenSelect.querySelector('option[value="low"]')?.textContent,
    ).toContain('1280×720 고정 / 10fps')

    fireEvent.change(fullscreenSelect, {
      target: { value: 'high' },
    })
    expect(mocks.session.setVideoProfile).toHaveBeenCalledWith('high')

    fireEvent.click(screen.getByTestId('fullscreen-capture-button'))
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(1, 'MetaLeft', 'down')
    expect(mocks.session.sendKey).toHaveBeenNthCalledWith(6, 'MetaLeft', 'up')
    expect(screen.queryByTestId('fullscreen-tools-drawer')).toBeNull()
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
