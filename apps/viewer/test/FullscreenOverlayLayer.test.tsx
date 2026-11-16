import {StrictMode} from 'react'
import {act, cleanup, render, screen} from '@testing-library/react'
import {afterEach, describe, expect, it, vi} from 'vitest'
import {FullscreenOverlayLayer, getFullscreenToolsRight} from '../src/FullscreenOverlayLayer'

const priorShow=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'showPopover')
const priorHide=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'hidePopover')
afterEach(()=>{
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  for(const [key,descriptor] of [['showPopover',priorShow],['hidePopover',priorHide]] as const){
    if(descriptor)Object.defineProperty(HTMLElement.prototype,key,descriptor)
    else Reflect.deleteProperty(HTMLElement.prototype,key)
  }
})

describe('fullscreen tools image-edge anchor',()=>{
  it.each([
    [3440,1440,1600,900,452],
    [1920,1080,1600,900,12],
    [2560,1080,1920,1080,332],
    [2293,960,1600,900,305 + 1/6],
    [1080,1920,1600,900,12],
    [3440,1440,900,1600,1327],
  ])('anchors viewport %ix%i with image %ix%i', (width,height,vw,vh,expected)=>{
    expect(getFullscreenToolsRight(width,{left:0,width,height},vw,vh)).toBeCloseTo(expected)
  })
  it('uses the native-size element rect rather than the viewport aspect ratio',()=>{
    expect(getFullscreenToolsRight(3440,{left:920,width:1600,height:900},1600,900)).toBe(932)
  })
  it('clamps clipped native video and tiny content to keep the palette on screen',()=>{
    expect(getFullscreenToolsRight(320,{left:-100,width:1600,height:900},1600,900)).toBe(12)
    expect(getFullscreenToolsRight(320,{left:0,width:10,height:10},10,10)).toBe(164)
  })
  it('falls back safely while metadata is unavailable or geometry is invalid',()=>{
    expect(getFullscreenToolsRight(3440,{left:0,width:3440,height:1440},0,0)).toBe(12)
    expect(getFullscreenToolsRight(3440,{left:NaN,width:3440,height:1440},1600,900)).toBe(12)
  })
  it('updates on video/window size changes and removes observers on exit',()=>{
    let resized: ResizeObserverCallback | undefined
    const disconnect=vi.fn()
    const observe=vi.fn()
    vi.stubGlobal('ResizeObserver',class {
      constructor(callback: ResizeObserverCallback){resized=callback}
      observe=observe
      disconnect=disconnect
    })
    const video=document.createElement('video')
    let width=3440, height=1440
    vi.spyOn(video,'getBoundingClientRect').mockImplementation(()=>({left:0,width,height} as DOMRect))
    Object.defineProperty(video,'videoWidth',{configurable:true,value:1600})
    Object.defineProperty(video,'videoHeight',{configurable:true,value:900})
    vi.stubGlobal('innerWidth',width)
    let frame: FrameRequestCallback | undefined
    vi.spyOn(window,'requestAnimationFrame').mockImplementation(callback=>{frame=callback;return 1})
    const cancel=vi.spyOn(window,'cancelAnimationFrame')
    const videoRef={current:video}
    const {rerender}=render(<FullscreenOverlayLayer active videoRef={videoRef}><button>tool</button></FullscreenOverlayLayer>)
    const layer=screen.getByTestId('fullscreen-overlay-layer')
    expect(layer.style.getPropertyValue('--fullscreen-tools-right')).toBe('452px')
    expect(observe).toHaveBeenCalledWith(video)
    width=1920; height=1080
    vi.stubGlobal('innerWidth',width)
    act(()=>{window.dispatchEvent(new Event('resize'));frame?.(0)})
    expect(layer.style.getPropertyValue('--fullscreen-tools-right')).toBe('12px')
    width=3440; height=1440
    vi.stubGlobal('innerWidth',width)
    act(()=>{resized?.([],{} as ResizeObserver);frame?.(0)})
    expect(layer.style.getPropertyValue('--fullscreen-tools-right')).toBe('452px')
    act(()=>video.dispatchEvent(new Event('resize')))
    rerender(<FullscreenOverlayLayer active={false} videoRef={videoRef}><button>tool</button></FullscreenOverlayLayer>)
    expect(disconnect).toHaveBeenCalledOnce()
    expect(cancel).toHaveBeenCalledWith(1)
    expect(layer.style.getPropertyValue('--fullscreen-tools-right')).toBe('')
  })
})
function mockAPI(fails=false){
  const show=vi.fn(()=>{if(fails)throw new DOMException('denied','InvalidStateError')})
  const hide=vi.fn()
  Object.defineProperty(HTMLElement.prototype,'showPopover',{configurable:true,value:show})
  Object.defineProperty(HTMLElement.prototype,'hidePopover',{configurable:true,value:hide})
  return {show,hide}
}
describe('nonmodal fullscreen overlay layer',()=>{
  it('does not enter the top layer in normal mode',()=>{
    const api=mockAPI()
    render(<FullscreenOverlayLayer active={false}><button>tool</button></FullscreenOverlayLayer>)
    expect(api.show).not.toHaveBeenCalled()
    expect(screen.getByTestId('fullscreen-overlay-layer').hasAttribute('popover')).toBe(false)
  })
  it('uses a manual popover without modal/focus-trap semantics',()=>{
    const api=mockAPI()
    render(<FullscreenOverlayLayer active><button>tool</button></FullscreenOverlayLayer>)
    const layer=screen.getByTestId('fullscreen-overlay-layer')
    expect(api.show).toHaveBeenCalledOnce()
    expect(layer.getAttribute('popover')).toBe('manual')
    expect(layer.dataset.layerMode).toBe('popover')
    expect(layer.hasAttribute('inert')).toBe(false)
    expect(layer.hasAttribute('aria-modal')).toBe(false)
    expect(document.activeElement).toBe(document.body)
  })
  it('preserves child identity and removes top-layer state on fullscreen exit',()=>{
    const api=mockAPI()
    const {rerender}=render(<FullscreenOverlayLayer active><button>tool</button></FullscreenOverlayLayer>)
    const button=screen.getByRole('button')
    rerender(<FullscreenOverlayLayer active={false}><button>tool</button></FullscreenOverlayLayer>)
    expect(api.hide).toHaveBeenCalledOnce()
    expect(screen.getByRole('button')).toBe(button)
    expect(screen.getByTestId('fullscreen-overlay-layer').hasAttribute('popover')).toBe(false)
  })
  it('falls back visibly if opening a popover throws',()=>{
    mockAPI(true)
    render(<FullscreenOverlayLayer active><button>tool</button></FullscreenOverlayLayer>)
    const layer=screen.getByTestId('fullscreen-overlay-layer')
    expect(layer.dataset.layerMode).toBe('fallback')
    expect(layer.hasAttribute('popover')).toBe(false)
  })
  it('falls back when the browser API is unavailable',()=>{
    Object.defineProperty(HTMLElement.prototype,'showPopover',{configurable:true,value:undefined})
    render(<FullscreenOverlayLayer active><button>tool</button></FullscreenOverlayLayer>)
    expect(screen.getByTestId('fullscreen-overlay-layer').dataset.layerMode).toBe('fallback')
  })
  it('cleans up correctly through StrictMode mount and unmount',()=>{
    const api=mockAPI()
    const {unmount}=render(<StrictMode><FullscreenOverlayLayer active><button>tool</button></FullscreenOverlayLayer></StrictMode>)
    expect(api.show).toHaveBeenCalledTimes(2)
    expect(api.hide).toHaveBeenCalledTimes(1)
    unmount();expect(api.hide).toHaveBeenCalledTimes(2)
  })
})
