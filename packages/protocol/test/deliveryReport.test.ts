import { describe,it,expect } from 'vitest'
import { validateControlMessage } from '../src/index'
const delivery={intervalMs:1000,receivedPackets:998,lostPackets:2,bufferMs:15,decodeMs:6,decodedFrames:15,droppedFrames:0}
const envelope=(data:unknown)=>({data,event:'video.receiver-report',sequence:1,
  sessionId:'session_0123456789abcdef',timestamp:Date.now(),version:1})
const base={jitterMs:6,packetLossPercent:.2,receivedBitrateMbps:1,route:'turn'}
describe('optional delivery counters',()=>{
  it('accepts enum-only path labels and legacy reports, rejecting address/auth fields',()=>{
    const path={localType:'relay',remoteType:'relay',candidateProtocol:'udp',relayProtocol:'tls'}
    const report=(data:unknown)=>({...envelope(data),event:'video.path-report'})
    expect(validateControlMessage(report(path)).ok).toBe(true)
    expect(validateControlMessage(envelope({...base,path})).ok).toBe(false)
    for(const fields of [{url:'turns:secret'},{relayProtocol:'quic'},{localType:'192.0.2.1'},{credential:'secret'}])
      expect(validateControlMessage(report({...path,...fields})).ok).toBe(false)
  })
  it('accepts legacy and new reports with bounded optional delivery',()=>{
    expect(validateControlMessage(envelope(base)).ok).toBe(true)
    expect(validateControlMessage(envelope({...base,delivery})).ok).toBe(true)
    expect(validateControlMessage(envelope({...base,delivery:{...delivery,receivedPackets:null,lostPackets:null}})).ok).toBe(true)
    expect(validateControlMessage(envelope({...base,delivery:{...delivery,recoveredPackets:0,receivedBytes:62000,controlRttMs:18}})).ok).toBe(true)
  })
  it('rejects extra, noninteger, nonfinite and oversized delivery fields',()=>{
    for(const fields of [{command:'run'},{intervalMs:0},{intervalMs:3001},{lostPackets:1.5},
      {lostPackets:true},{receivedPackets:1000001},{decodeMs:NaN},{bufferMs:Infinity},{decodedFrames:-1},
      {recoveredPackets:-1},{receivedBytes:67108865},{controlRttMs:Infinity}])
      expect(validateControlMessage(envelope({...base,delivery:{...delivery,...fields}})).ok).toBe(false)
  })
})
