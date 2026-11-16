import { describe, it, expect } from 'vitest'
import { computeDeliverySample, computeVideoTelemetry, type VideoStatsSample } from '../src/viewerMetrics'

const diagnostics = { bufferMs: 12, decodeMs: 5, droppedFrames: 0, controlBufferedBytes: 0, controlPeakBytes: 0 }
const first: VideoStatsSample = { streamId:'stream-one',timestampMs:1000,bytesReceived:10000,
  framesDecoded:15,framesDropped:0,framesPerSecond:15,frameWidth:1600,frameHeight:900,
  packetsReceived:100,packetsLost:2,jitterSeconds:.006,qpSum:300 }
const next = (fields: Partial<VideoStatsSample> = {}): VideoStatsSample => ({...first,
  timestampMs:2000,framesDecoded:30,packetsReceived:200,packetsLost:3,...fields})

describe('bounded delivery count deltas',()=>{
  it('transmits real interval and packet counts with timing diagnostics',()=>{
    expect(computeDeliverySample(first,next(),diagnostics)).toEqual({intervalMs:1000,
      receivedPackets:100,lostPackets:1,recoveredPackets:0,receivedBytes:0,controlRttMs:null,
      bufferMs:12,decodeMs:5,decodedFrames:15,droppedFrames:0})
  })
  it('does not cross a new stream, first sample, cache, backward clock or gap',()=>{
    for(const fields of [{streamId:'another'}, {timestampMs:1000}, {timestampMs:900}, {timestampMs:5000}])
      expect(computeDeliverySample(first,next(fields),diagnostics)).toBeUndefined()
    expect(computeDeliverySample(null,next(),diagnostics)).toBeUndefined()
  })
  it('keeps reset and missing counters unknown rather than zero loss',()=>{
    for(const value of [null,NaN,Infinity,2.5]) {
      const result=computeDeliverySample(first,next({packetsLost:value}),diagnostics)
      expect(result?.lostPackets).toBeNull()
    }
    expect(computeDeliverySample(first,next({packetsReceived:1}),diagnostics)?.receivedPackets).toBeNull()
  })
  it('reports recovered packets only with monotonic received/byte counters',()=>{
    expect(computeDeliverySample(first,next({packetsLost:0,bytesReceived:12000}),diagnostics,18)).toMatchObject({
      lostPackets:0,recoveredPackets:2,receivedPackets:100,receivedBytes:2000,controlRttMs:18})
    for(const fields of [{packetsLost:-200},{packetsLost:0,bytesReceived:5},{packetsLost:0,packetsReceived:100}])
      expect(computeDeliverySample(first,next(fields),diagnostics)?.lostPackets).toBeNull()
  })
  it('does not invent a loss rate across reset or zero-time samples',()=>{
    expect(computeVideoTelemetry(first,next({packetsReceived:0})).packetLossPercent).toBeNull()
    expect(computeVideoTelemetry(first,next({timestampMs:1000})).packetLossPercent).toBeNull()
    expect(computeVideoTelemetry(first,next({streamId:'another'})).receivedBitrateMbps).toBeNull()
  })
  it('sanitizes nonfinite/negative diagnostic values',()=>{
    const value=computeDeliverySample(first,next(),{...diagnostics,bufferMs:NaN,decodeMs:-1})
    expect(value?.bufferMs).toBeNull(); expect(value?.decodeMs).toBeNull()
  })
})
