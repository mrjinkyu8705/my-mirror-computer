import { createSessionTicket } from '@mirror/protocol'
import { describe, expect, it } from 'vitest'
import { createDeviceToken } from '../src/deviceToken'
import { handleProductionWebSocketUpgrade } from '../src/productionWebSocket'
import { deriveTrialViewerKey } from '../src/trialViewer'
import { handleTurnRequest } from '../src/turnCredentials'

const config = {
  viewerOrigin: 'https://viewer.example.com',
  trialViewerOrigin: 'https://trial.example.com',
  ticketSecret: 'sample-production-session-key-0123456789',
  deviceAuthSecret: 'sample-device-key-0123456789abcdef',
}

const claims = {
  deviceId: 'device_0123456789abcdef', nonce: 'nonce_0123456789abcdef',
  sessionId: 'session_0123456789abcdef', sub: 'owner@example.com',
}

async function tickets() {
  const trialKey = await deriveTrialViewerKey(config.ticketSecret, config.trialViewerOrigin)
  return {
    live: await createSessionTicket({ ...claims, permission: 'control', secret: config.ticketSecret }),
    trial: await createSessionTicket({ ...claims, permission: 'control', secret: trialKey }),
    device: await createDeviceToken({ ...claims, secret: config.deviceAuthSecret }),
  }
}

function request(path: string, origin?: string, ticket?: string) {
  const url = new URL(path, 'https://signal.example.com')
  if (ticket) url.searchParams.set('ticket', ticket)
  return new Request(url, { headers: origin ? { Origin: origin } : {} })
}

describe('isolated viewer origin', () => {
  it('separates production, trial and agent credentials at WebSocket upgrade', async () => {
    const t = await tickets()
    const cases = [
      [config.viewerOrigin, t.live, 200], [config.trialViewerOrigin, t.trial, 200],
      [config.viewerOrigin, t.trial, 401], [config.trialViewerOrigin, t.live, 401],
      [config.trialViewerOrigin, t.device, 401], [config.trialViewerOrigin, undefined, 401],
      ['https://trial.example.com.evil.test', t.trial, 403], ['null', t.trial, 403],
      [undefined, t.device, 200], [undefined, t.trial, 401],
    ] as const
    for (const [origin, ticket, status] of cases) {
      const response = await handleProductionWebSocketUpgrade(
        request('/ws', origin, ticket), config, async () => new Response(null),
      )
      expect(response.status, `${origin ?? 'agent'} should give ${status}`).toBe(status)
    }
  })

  it('returns TURN credentials only with a ticket for the appropriate origin', async () => {
    const t = await tickets()
    for (const [origin, ticket, status, cors] of [
      [config.viewerOrigin, t.live, 200, config.viewerOrigin],
      [config.trialViewerOrigin, t.trial, 200, config.trialViewerOrigin],
      [config.trialViewerOrigin, t.live, 401, config.trialViewerOrigin],
      [config.trialViewerOrigin, t.device, 401, config.trialViewerOrigin],
      [config.viewerOrigin, t.trial, 401, config.viewerOrigin],
      ['https://other.example.com', t.trial, 401, config.viewerOrigin],
      [undefined, t.device, 200, config.viewerOrigin],
      [undefined, t.trial, 401, config.viewerOrigin],
    ] as const) {
      const response = await handleTurnRequest(request('/turn', origin, ticket), config)
      expect(response.status).toBe(status)
      expect(response.headers.get('access-control-allow-origin')).toBe(cors)
      expect(response.headers.get('vary')).toBe('Origin')
    }
  })

  it('keeps trial disabled without its exact configured origin', async () => {
    const t = await tickets()
    const withoutTrial = { ...config, trialViewerOrigin: undefined }
    const result = await handleProductionWebSocketUpgrade(
      request('/ws', config.trialViewerOrigin, t.trial), withoutTrial, async () => new Response(null),
    )
    expect(result.status).toBe(403)
  })
})
