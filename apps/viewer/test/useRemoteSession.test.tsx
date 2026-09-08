/** @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  CONNECTION_ESTABLISHMENT_TIMEOUT_MS,
  useRemoteSession,
} from '../src/useRemoteSession'

const requestSessionConfig = vi.hoisted(() => vi.fn())

vi.mock('../src/productionSession', () => ({
  isProductionHost: () => true,
  requestSessionConfig,
}))

class FakeWebSocket extends EventTarget {
  static readonly CLOSED = 3
  static readonly CLOSING = 2
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static instances: FakeWebSocket[] = []

  readonly sent: string[] = []
  readyState = FakeWebSocket.CONNECTING

  constructor(readonly url: string | URL) {
    super()
    FakeWebSocket.instances.push(this)
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.dispatchEvent(new Event('open'))
  }

  message(data: string): void {
    this.dispatchEvent(new MessageEvent('message', { data }))
  }

  send(data: string): void {
    if (this.readyState !== FakeWebSocket.OPEN) {
      throw new DOMException('socket is not open', 'InvalidStateError')
    }
    this.sent.push(data)
  }

  close(_code?: number, _reason?: string): void {
    if (this.readyState === FakeWebSocket.CLOSED) {
      return
    }
    this.readyState = FakeWebSocket.CLOSED
    this.dispatchEvent(new Event('close'))
  }
}

class FakeDataChannel extends EventTarget {
  readyState: RTCDataChannelState = 'connecting'

  send(_data: string): void {}

  close(): void {
    this.readyState = 'closed'
    this.dispatchEvent(new Event('close'))
  }
}

class OfferFailingPeerConnection extends EventTarget {
  connectionState: RTCPeerConnectionState = 'new'
  iceGatheringState: RTCIceGatheringState = 'new'
  localDescription: RTCSessionDescription | null = null

  addTransceiver(): RTCRtpTransceiver {
    return {} as RTCRtpTransceiver
  }

  createDataChannel(): RTCDataChannel {
    return new FakeDataChannel() as unknown as RTCDataChannel
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    throw new Error('offer failed')
  }

  async setLocalDescription(
    _description?: RTCLocalSessionDescriptionInit,
  ): Promise<void> {}

  close(): void {
    this.connectionState = 'closed'
  }
}

describe('useRemoteSession connection establishment watchdog', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    requestSessionConfig.mockReset()
    requestSessionConfig.mockResolvedValue({
      deviceId: 'device_0123456789abcdef',
      iceServers: [],
      sessionId: 'session_0123456789abcdef',
      ticket: 'ticket',
      webSocketUrl: 'wss://signaling.example/ws',
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('starts with the balanced video profile selected', () => {
    const { result, unmount } = renderHook(() => useRemoteSession())

    expect(result.current.videoProfile).toBe('balanced')
    unmount()
  })

  it('leaves a stalled reserved session and exposes a retryable timeout', async () => {
    const { result, unmount } = renderHook(() => useRemoteSession())

    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    expect(FakeWebSocket.instances).toHaveLength(1)

    act(() => FakeWebSocket.instances[0]?.open())
    expect(result.current.connectionState).toBe('reserved')

    act(() => vi.advanceTimersByTime(CONNECTION_ESTABLISHMENT_TIMEOUT_MS))

    expect(result.current.connectionState).toBe('offline')
    expect(result.current.errorMessage).toContain('30초')
    expect(result.current.canRetry).toBe(true)
    unmount()
  })

  it('also bounds a production ticket request that never settles', () => {
    requestSessionConfig.mockReturnValue(new Promise(() => undefined))
    const { result, unmount } = renderHook(() => useRemoteSession())

    act(() => result.current.connect())
    act(() => vi.advanceTimersByTime(CONNECTION_ESTABLISHMENT_TIMEOUT_MS))

    expect(result.current.connectionState).toBe('offline')
    expect(result.current.errorMessage).toContain('30초')
    expect(result.current.canRetry).toBe(true)
    expect(FakeWebSocket.instances).toHaveLength(0)
    unmount()
  })

  it('cancels the watchdog when the user disconnects', async () => {
    const { result, unmount } = renderHook(() => useRemoteSession())

    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    act(() => FakeWebSocket.instances[0]?.open())
    act(() => result.current.disconnect())

    act(() =>
      vi.advanceTimersByTime(
        CONNECTION_ESTABLISHMENT_TIMEOUT_MS + 2_000,
      ),
    )

    expect(result.current.connectionState).toBe('offline')
    expect(result.current.errorMessage).toBeNull()
    expect(FakeWebSocket.instances).toHaveLength(1)
    unmount()
  })

  it('turns a valid-message negotiation exception into an automatic retry', async () => {
    vi.stubGlobal('RTCPeerConnection', OfferFailingPeerConnection)
    const { result, unmount } = renderHook(() => useRemoteSession())

    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    const socket = FakeWebSocket.instances[0]
    act(() => socket?.open())
    act(() =>
      socket?.message(
        JSON.stringify({
          payload: {
            expiresAt: Date.now() + 60_000,
            permission: 'control',
            videoProfile: 'high',
          },
          sequence: 1,
          sessionId: 'session_0123456789abcdef',
          type: 'session.accept',
          version: 1,
        }),
      ),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(result.current.connectionState).toBe('offline')
    expect(result.current.errorMessage).toContain('협상')
    expect(result.current.canRetry).toBe(true)

    await act(async () => {
      vi.advanceTimersByTime(2_000)
      await Promise.resolve()
    })
    expect(FakeWebSocket.instances).toHaveLength(2)
    unmount()
  })

  it('does not automatically reclaim a session replaced by another viewer', async () => {
    const { result, unmount } = renderHook(() => useRemoteSession())

    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    const socket = FakeWebSocket.instances[0]
    act(() => socket?.open())

    act(() =>
      socket?.message(
        JSON.stringify({
          payload: {
            code: 'SESSION_REPLACED',
            retryable: true,
          },
          sequence: 0,
          sessionId: 'session_0123456789abcdef',
          type: 'error',
          version: 1,
        }),
      ),
    )

    expect(result.current.connectionState).toBe('offline')
    expect(result.current.errorMessage).toContain('다른 기기·탭')
    expect(result.current.canRetry).toBe(true)

    await act(async () => {
      vi.advanceTimersByTime(2_500)
      await Promise.resolve()
    })
    expect(FakeWebSocket.instances).toHaveLength(1)

    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    expect(FakeWebSocket.instances).toHaveLength(2)
    unmount()
  })
})
