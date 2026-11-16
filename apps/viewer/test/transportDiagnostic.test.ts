import { describe, it, expect } from 'vitest'
import { diagnosticIceConfiguration } from '../src/transportDiagnostic'

const servers = [{ urls: 'stun:stun.example:3478' }, {
  urls: ['turn:turn.example:3478?transport=udp', 'turn:turn.example:3478?transport=tcp',
    'turns:turn.example:5349?transport=tcp'], username: 'fixture-user', credential: 'fixture-secret',
}]

describe('URL-scoped TURN diagnostics', () => {
  it('preserves ordinary configuration and server ordering by identity', () => {
    expect(diagnosticIceConfiguration(servers, '')).toEqual({iceServers:servers})
    expect(diagnosticIceConfiguration(servers, '?foo=tls').iceServers).toBe(servers)
  })
  it.each(['udp','tcp','tls'])('selects only provided %s URLs and forces relay for this peer', (mode) => {
    const original = JSON.stringify(servers)
    const result = diagnosticIceConfiguration(servers, `?diagnosticTurn=${mode}`)
    expect(result.iceTransportPolicy).toBe('relay')
    expect(result.iceServers).toHaveLength(1)
    expect(result.iceServers?.[0]?.urls).toEqual([servers[1]!.urls![mode==='udp'?0:mode==='tcp'?1:2]])
    expect(result.iceServers?.[0]?.credential).toBe('fixture-secret')
    expect(JSON.stringify(servers)).toBe(original)
    expect(diagnosticIceConfiguration(servers,'')).toEqual({iceServers:servers})
  })
  it('fails closed for unsupported trials instead of silently using direct or UDP', () => {
    for(const query of ['?diagnosticTurn=bad','?diagnosticTurn=','?diagnosticTurn=tls&diagnosticTurn=udp'])
      expect(()=>diagnosticIceConfiguration(servers,query)).toThrow()
    expect(()=>diagnosticIceConfiguration([servers[0]!],'?diagnosticTurn=tls')).toThrow()
  })
  it('recognizes default TURN transports without inventing endpoints', () => {
    expect(diagnosticIceConfiguration([{urls:['turn:x','turns:x']}], '?diagnosticTurn=tls')
      .iceServers?.[0]?.urls).toEqual(['turns:x'])
  })
})
