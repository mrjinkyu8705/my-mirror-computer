import { type ReactNode, type RefObject, useLayoutEffect, useRef } from 'react'

/** Right inset for a 144px palette, using the visible object-fit:contain image. */
export function getFullscreenToolsRight(
  viewportWidth: number,
  rect: Pick<DOMRect, 'left' | 'width' | 'height'>,
  videoWidth: number,
  videoHeight: number,
): number {
  const inset = 12
  if (![viewportWidth, rect.left, rect.width, rect.height, videoWidth, videoHeight].every(Number.isFinite) ||
      viewportWidth <= 0 || rect.width <= 0 || rect.height <= 0 || videoWidth <= 0 || videoHeight <= 0) return inset
  const scale = Math.min(rect.width / videoWidth, rect.height / videoHeight)
  const imageRight = rect.left + (rect.width + videoWidth * scale) / 2
  // Native-size/scroll mode can extend beyond the viewport; keep both controls
  // on screen. The same geometry handles portrait letterboxing and DPR/zoom.
  return Math.min(Math.max(inset, viewportWidth - imageRight + inset), Math.max(inset, viewportWidth - 156))
}

/** Nonmodal top-layer UI above fullscreen video; regular layout outside it. */
export function FullscreenOverlayLayer({active, children, videoRef}: {
  readonly active: boolean
  readonly children: ReactNode
  readonly videoRef?: RefObject<HTMLVideoElement | null>
}) {
  const root = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const layer = root.current
    if (!layer || !active) return
    layer.dataset.layerMode = 'fallback'
    if (typeof layer.showPopover === 'function' && typeof layer.hidePopover === 'function') {
      layer.setAttribute('popover', 'manual')
      try {
        layer.showPopover()
        layer.dataset.layerMode = 'popover'
      } catch {
        // Unsupported/denied top-layer rendering must not leave the controls
        // hidden by a closed popover. Keep the fixed composited fallback.
        layer.removeAttribute('popover')
      }
    }
    return () => {
      if (layer.hasAttribute('popover')) {
        try { layer.hidePopover() } catch { /* Browser may already have closed it. */ }
        layer.removeAttribute('popover')
      }
      delete layer.dataset.layerMode
    }
  }, [active])

  useLayoutEffect(() => {
    const layer = root.current
    if (!layer || !active) return
    const video = videoRef?.current
    const stage = video?.parentElement
    let frame: number | null = null
    const update = () => {
      frame = null
      const right = video
        ? getFullscreenToolsRight(layer.clientWidth || window.innerWidth,
            video.getBoundingClientRect(), video.videoWidth, video.videoHeight)
        : 12
      layer.style.setProperty('--fullscreen-tools-right', `${right}px`)
    }
    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(update)
    }
    update()
    // Event-driven measurements only: no video frame polling or interval.
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null
    observer?.observe(layer)
    if (video) observer?.observe(video)
    window.addEventListener('resize', schedule)
    window.visualViewport?.addEventListener('resize', schedule)
    for (const event of ['loadedmetadata', 'resize', 'emptied']) video?.addEventListener(event, schedule)
    stage?.addEventListener('scroll', schedule)
    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame)
      observer?.disconnect()
      window.removeEventListener('resize', schedule)
      window.visualViewport?.removeEventListener('resize', schedule)
      for (const event of ['loadedmetadata', 'resize', 'emptied']) video?.removeEventListener(event, schedule)
      stage?.removeEventListener('scroll', schedule)
      layer.style.removeProperty('--fullscreen-tools-right')
    }
  }, [active, videoRef])

  return <div ref={root} className="fullscreen-overlay-layer"
    data-testid="fullscreen-overlay-layer" data-fullscreen={active}>
    {children}
  </div>
}
