import { afterEach, describe, expect, it, vi } from 'vitest'
import { BULK_BUFFER_BYTES, BULK_CHUNK_BYTES, waitForBulkCapacity } from '../src/bulkTransfer'

afterEach(() => vi.useRealTimers())
const channel = () => ({ readyState:'open', bufferedAmount:0 } as unknown as RTCDataChannel)

describe('cooperative bulk admission', () => {
  it('yields a browser task even when the channel is empty', async () => {
    vi.useFakeTimers()
    let ready=false
    const result=waitForBulkCapacity(channel(),100,()=>false).then(ok=>{ready=ok;return ok})
    await Promise.resolve();expect(ready).toBe(false)
    await vi.advanceTimersByTimeAsync(4)
    expect(await result).toBe(true)
  })

  it('bounds the aggregate of simultaneous file/image/text queues', async () => {
    vi.useFakeTimers()
    let peerBytes=BULK_BUFFER_BYTES
    let ready=false
    const result=waitForBulkCapacity(channel(),BULK_CHUNK_BYTES,()=>false,
      {bufferedBytes:()=>peerBytes,controlBufferedBytes:()=>0}).then(ok=>{ready=ok;return ok})
    await vi.advanceTimersByTimeAsync(20);expect(ready).toBe(false)
    peerBytes=BULK_BUFFER_BYTES-BULK_CHUNK_BYTES
    await vi.advanceTimersByTimeAsync(10);expect(await result).toBe(true)
  })

  it('withholds bulk while control is queued, then resumes', async () => {
    vi.useFakeTimers()
    let controlBytes=2048,ready=false
    const result=waitForBulkCapacity(channel(),100,()=>false,
      {bufferedBytes:()=>0,controlBufferedBytes:()=>controlBytes}).then(ok=>{ready=ok;return ok})
    await vi.advanceTimersByTimeAsync(20);expect(ready).toBe(false)
    controlBytes=0
    await vi.advanceTimersByTimeAsync(10);expect(await result).toBe(true)
  })

  it.each(['cancel','close'] as const)('rechecks %s during its yield', async reason => {
    vi.useFakeTimers()
    const ch=channel();let cancelled=false
    const result=waitForBulkCapacity(ch,100,()=>cancelled)
    if(reason==='cancel')cancelled=true
    else Object.assign(ch,{readyState:'closed'})
    await vi.advanceTimersByTimeAsync(4);expect(await result).toBe(false)
  })

  it('rejects invalid chunk sizes and bounds a permanent stall', async () => {
    vi.useFakeTimers()
    await expect(waitForBulkCapacity(channel(),BULK_CHUNK_BYTES+1,()=>false)).rejects.toThrow('INVALID_CHUNK')
    const result=waitForBulkCapacity(channel(),100,()=>false,
      {bufferedBytes:()=>BULK_BUFFER_BYTES,controlBufferedBytes:()=>0})
    await vi.advanceTimersByTimeAsync(30_020)
    expect(await result).toBe(false)
  })
})
