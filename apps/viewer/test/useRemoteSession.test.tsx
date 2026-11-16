/** @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react'
import { webcrypto } from 'node:crypto'
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
  bufferedAmount = 0
  readonly sent: { event: string; data: Record<string, unknown>; sequence: number }[] = []
  readonly chunks: ArrayBuffer[] = []

  send(data: string | ArrayBuffer): void {
    if (typeof data === 'string') this.sent.push(JSON.parse(data))
    else this.chunks.push(data)
  }

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

class ConnectedPeer extends OfferFailingPeerConnection {
  static latest: ConnectedPeer
  override iceGatheringState: RTCIceGatheringState = 'complete'
  readonly channels: FakeDataChannel[] = []
  constructor() { super(); ConnectedPeer.latest = this }
  override createDataChannel(): RTCDataChannel {
    const channel = new FakeDataChannel()
    this.channels.push(channel)
    return channel as unknown as RTCDataChannel
  }
  override async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'v=0\r\n' }
  }
  override async setLocalDescription(description: RTCLocalSessionDescriptionInit): Promise<void> {
    this.localDescription = description as RTCSessionDescription
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

  it.each([false,true])('keeps normal routing or applies only the explicitly requested viewer TLS trial (%s)', async (trial) => {
    let captured: RTCConfiguration | undefined
    class ConfiguredPeer extends ConnectedPeer {
      constructor(configuration?: RTCConfiguration) { super(); captured=configuration }
    }
    vi.stubGlobal('RTCPeerConnection',ConfiguredPeer)
    window.history.replaceState({},'',trial?'/?diagnosticTurn=tls':'/')
    const iceServers=[{urls:['turn:turn.example:3478?transport=udp','turns:turn.example:443?transport=tcp'],
      username:'fixture',credential:'fixture'}]
    requestSessionConfig.mockResolvedValue({deviceId:'device_0123456789abcdef',iceServers,
      sessionId:'session_0123456789abcdef',ticket:'ticket',webSocketUrl:'wss://signaling.example/ws'})
    const {result,unmount}=renderHook(()=>useRemoteSession())
    try {
      act(()=>result.current.connect())
      await act(async()=>Promise.resolve())
      const socket=FakeWebSocket.instances[0]!
      act(()=>socket.open())
      await act(async()=>{
        socket.message(JSON.stringify({payload:{expiresAt:Date.now()+60000,permission:'control'},
          sequence:1,sessionId:'session_0123456789abcdef',type:'session.accept',version:1}))
        await Promise.resolve();await Promise.resolve()
      })
      expect(captured?.iceTransportPolicy).toBe(trial?'relay':undefined)
      expect(captured?.iceServers).toEqual(trial?[{...iceServers[0],urls:[iceServers[0]!.urls[1]]}]:[
        {urls:'stun:stun.cloudflare.com:3478'},...iceServers])
      expect(result.current.videoProfile).toBe('balanced')
    } finally { unmount();window.history.replaceState({},'','/') }
  })

  it('preserves click coordinates and key-up order under control backpressure, then clears pending input', async () => {
    vi.stubGlobal('RTCPeerConnection', ConnectedPeer)
    const { result, unmount } = renderHook(() => useRemoteSession())
    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    const socket = FakeWebSocket.instances[0]!
    act(() => socket.open())
    await act(async () => {
      socket.message(JSON.stringify({ payload: { expiresAt: Date.now() + 60000, permission: 'control' },
        sequence: 1, sessionId: 'session_0123456789abcdef', type: 'session.accept', version: 1 }))
      await Promise.resolve()
      await Promise.resolve()
    })
    const peer = ConnectedPeer.latest
    const control = peer.channels[0]!
    act(() => {
      peer.connectionState = 'connected'
      control.readyState = 'open'
      control.dispatchEvent(new Event('open'))
      peer.dispatchEvent(Object.assign(new Event('track'), { streams: [{}] }))
    })
    expect(result.current.isControlActive).toBe(true)
    control.sent.length = 0
    control.bufferedAmount = 5000
    act(() => {
      result.current.sendPointerMove(0.1, 0.1)
      result.current.sendPointerMove(0.8, 0.9)
      result.current.sendPointerButton('left', 'down')
      result.current.sendKey('ShiftLeft', 'down')
      result.current.sendKey('KeyA', 'down')
      result.current.sendKey('KeyA', 'up')
      result.current.sendKey('ShiftLeft', 'up')
      result.current.sendPointerButton('left', 'up')
    })
    expect(control.sent.map(x => x.event)).toEqual(['pointer.move', 'pointer.button', 'key.down', 'key.down', 'key.up', 'key.up', 'pointer.button'])
    expect(control.sent[0]?.data).toEqual({ x: 0.8, y: 0.9 })
    expect(control.sent.every((value, i) => i === 0 || value.sequence > control.sent[i - 1]!.sequence)).toBe(true)
    act(() => {
      result.current.sendPointerMove(0.5, 0.5)
      result.current.releaseRemoteInput()
      control.bufferedAmount = 0
      control.dispatchEvent(new Event('bufferedamountlow'))
    })
    expect(control.sent.at(-1)?.event).toBe('control.release-all')
    const count = control.sent.length
    act(() => {
      control.bufferedAmount = 256 * 1024
      result.current.sendKey('KeyB', 'down')
    })
    expect(result.current.isControlActive).toBe(false)
    expect(control.sent.slice(count).some(x => x.event === 'key.down')).toBe(false)
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

  it('opens a separate bulk text channel and resolves only after its applied reply, then clears readiness on disconnect', async () => {
    vi.stubGlobal('crypto', webcrypto)
    vi.stubGlobal('RTCPeerConnection', ConnectedPeer)
    const { result, unmount } = renderHook(() => useRemoteSession())
    act(() => result.current.connect())
    await act(async () => Promise.resolve())
    const socket = FakeWebSocket.instances[0]!
    act(() => socket.open())
    await act(async () => {
      socket.message(JSON.stringify({ payload: { expiresAt: Date.now()+60000, permission: 'control' },
        sequence: 1, sessionId: 'session_0123456789abcdef', type: 'session.accept', version: 1 }))
      await Promise.resolve(); await Promise.resolve()
    })
    const peer = ConnectedPeer.latest, control = peer.channels[0]!, textChannel = peer.channels[1]!
    expect(result.current.canSendClipboardText).toBe(false)
    act(() => {
      peer.connectionState = 'connected'
      control.readyState = 'open';control.dispatchEvent(new Event('open'))
      peer.dispatchEvent(Object.assign(new Event('track'), { streams: [{}] }))
      textChannel.readyState = 'open';textChannel.dispatchEvent(new Event('open'))
    })
    expect(result.current.canSendClipboardText).toBe(true)
    let done = false
    const completion = result.current.setRemoteClipboard('한'.repeat(85000)).then(() => { done = true })
    await vi.waitFor(() => expect(textChannel.sent.length).toBe(1))
    expect(control.sent.some(message => message.event === 'clipboard.set')).toBe(false)
    const id = textChannel.sent[0]!.data.transferId
    const reply = (event: string, sequence: number) => textChannel.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ event, data: { transferId: id }, sequence,
        sessionId: 'session_0123456789abcdef', timestamp: Date.now(), version: 1 }),
    }))
    await act(async () => { reply('clipboard.text-accept', 1); await Promise.resolve() })
    await act(async () => { await vi.waitFor(() =>
      expect(textChannel.sent.some(message => message.event === 'clipboard.text-complete')).toBe(true)) })
    expect(textChannel.chunks.reduce((sum, chunk) => sum+chunk.byteLength, 0)).toBe(255000)
    expect(done).toBe(false)
    await act(async () => { reply('clipboard.text-applied', 2); await completion })
    expect(done).toBe(true)
    act(() => result.current.disconnect())
    expect(result.current.canSendClipboardText).toBe(false)
    await expect(result.current.setRemoteClipboard('old connection')).rejects.toThrow('CONTROL_INACTIVE')
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
