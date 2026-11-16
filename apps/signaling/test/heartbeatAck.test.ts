import { afterEach,describe,it,expect,vi } from 'vitest'

vi.mock('cloudflare:workers',()=>({DurableObject:class {
  constructor(readonly ctx:any,readonly env:any){}
}}))
import { SessionRoom } from '../src/sessionRoom'

const session='session_0123456789abcdef'
const heartbeat={type:'agent.heartbeat',payload:{},sequence:42,sessionId:session,version:1}
const attachment=(heartbeatAck:boolean)=>({role:'agent',deviceId:'device_0123456789abcdef',
  subject:'fixture-owner',sessionId:session,lastSeenAt:1,connectedAt:1,heartbeatAck})
class Socket {
  state:any
  readyState=1
  send=vi.fn()
  close=vi.fn()
  constructor(state:any=null){this.state=state}
  deserializeAttachment(){return this.state}
  serializeAttachment(state:any){this.state=state}
}
function room(){
  const ctx={storage:{getAlarm:vi.fn().mockResolvedValue(null),setAlarm:vi.fn().mockResolvedValue(undefined)},getWebSockets:vi.fn(()=>[]),acceptWebSocket:vi.fn()}
  return {ctx,value:new SessionRoom(ctx as any,{} as any)}
}
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers()})
describe('negotiated authenticated application heartbeat acknowledgement',()=>{
  it.each([false,true])('replies only when agent negotiated capability (%s)',async(enabled)=>{
    const {ctx,value}=room(),socket=new Socket(attachment(enabled))
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    expect(ctx.storage.setAlarm).toHaveBeenCalledOnce()
    expect(socket.send).toHaveBeenCalledTimes(enabled?1:0)
    if(enabled)expect(JSON.parse(socket.send.mock.calls[0]![0])).toEqual({...heartbeat,type:'agent.heartbeat-ack'})
    expect(ctx.getWebSockets).not.toHaveBeenCalled()
  })
  it.each(['agent','viewer'])('does not accept spoofed ACKs from %s',async(role)=>{
    const {value}=room(),socket=new Socket({...attachment(true),role})
    await value.webSocketMessage(socket as any,JSON.stringify({...heartbeat,type:'agent.heartbeat-ack'}))
    expect(socket.close).toHaveBeenCalledWith(1008,'MESSAGE_NOT_ALLOWED')
  })
  it('advertises capability only after an agent explicitly requests it',async()=>{
    const NativeResponse=Response
    vi.stubGlobal('Response',class {
      status:number;headers:Headers;webSocket:unknown
      constructor(_body:any,init:any){this.status=init.status;this.headers=new Headers(init.headers);this.webSocket=init.webSocket}
      static json=NativeResponse.json
    })
    vi.stubGlobal('WebSocketPair',class {0=new Socket();1=new Socket()})
    for(const negotiated of [false,true]){
      const {value}=room()
      const response=await value.fetch(new Request('https://room/connect',{headers:{upgrade:'websocket',
        'x-mirror-device-id':'device_0123456789abcdef','x-mirror-role':'agent',
        'x-mirror-session-id':session,'x-mirror-subject':'fixture-owner',
        ...(negotiated?{'x-mirror-app-heartbeat':'ack-v1'}:{})}}))
      expect(response.status).toBe(101)
      expect(response.headers.get('x-mirror-app-heartbeat')).toBe(negotiated?'ack-v1':null)
    }
  })
})

describe('coalesced persistent heartbeat alarm',()=>{
  function setup(){
    vi.useFakeTimers();vi.setSystemTime(100_000)
    const result=room(),socket=new Socket(attachment(true))
    result.ctx.getWebSockets.mockImplementation(()=>[socket] as any)
    vi.stubGlobal('WebSocket',{OPEN:1,CONNECTING:0})
    return {...result,socket}
  }
  it('keeps a future alarm and avoids storage on subsequent heartbeat ACKs',async()=>{
    const {ctx,value,socket}=setup()
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    // If a storage operation were attempted here, it would stall indefinitely.
    ctx.storage.setAlarm.mockImplementation(()=>new Promise(()=>{}))
    for(let i=1;i<=9;i++){
      vi.setSystemTime(100_000+i*3_000)
      await value.webSocketMessage(socket as any,JSON.stringify({...heartbeat,sequence:42+i}))
    }
    expect(ctx.storage.setAlarm).toHaveBeenCalledOnce()
    expect(socket.send).toHaveBeenCalledTimes(10)
    expect(socket.state.lastSeenAt).toBe(127_000)
  })
  it('rearms a fired alarm to last heartbeat plus the unchanged 30s timeout',async()=>{
    const {ctx,value,socket}=setup()
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    vi.setSystemTime(127_000)
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    vi.setSystemTime(130_000);await value.alarm()
    expect(ctx.storage.setAlarm).toHaveBeenLastCalledWith(157_000)
    expect(socket.close).not.toHaveBeenCalled()
    vi.setSystemTime(157_000);await value.alarm()
    expect(socket.close).toHaveBeenCalledWith(1012,'Agent heartbeat timeout')
  })
  it('rearms even when the existing alarm is invoked early or retried',async()=>{
    const {ctx,value,socket}=setup()
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    vi.setSystemTime(120_000);await value.alarm();await value.alarm()
    expect(ctx.storage.setAlarm).toHaveBeenCalledTimes(3)
    expect(ctx.storage.setAlarm).toHaveBeenLastCalledWith(130_000)
    expect(socket.close).not.toHaveBeenCalled()
  })
  it('cold start reuses the persisted earlier alarm without rewriting it',async()=>{
    const {ctx,value,socket}=setup()
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    ctx.storage.getAlarm.mockResolvedValue(130_000)
    const cold=new SessionRoom(ctx as any,{} as any)
    vi.setSystemTime(103_000);await cold.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    expect(ctx.storage.setAlarm).toHaveBeenCalledOnce()
    expect(ctx.storage.setAlarm).toHaveBeenLastCalledWith(130_000)
  })
  it('failed persistent write invalidates cache and must be retried',async()=>{
    const {ctx,value,socket}=setup()
    ctx.storage.setAlarm.mockRejectedValueOnce(new Error('fixture write failure'))
    await expect(value.webSocketMessage(socket as any,JSON.stringify(heartbeat))).rejects.toThrow('fixture write failure')
    expect(socket.send).not.toHaveBeenCalled()
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    expect(ctx.storage.setAlarm).toHaveBeenCalledTimes(2)
    expect(socket.send).toHaveBeenCalledOnce()
  })
  it('does not strand the timer if a heartbeat arrives after the cached deadline',async()=>{
    const {ctx,value,socket}=setup()
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    vi.setSystemTime(131_000)
    await value.webSocketMessage(socket as any,JSON.stringify(heartbeat))
    expect(ctx.storage.setAlarm).toHaveBeenLastCalledWith(161_000)
  })
})
