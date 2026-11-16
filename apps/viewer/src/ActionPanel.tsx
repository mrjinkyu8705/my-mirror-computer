import { type ReactNode, useEffect, useRef } from 'react'
import { IconButton } from './IconButton'

interface Props {
  readonly open: boolean
  readonly title: string
  readonly subtitle: string
  readonly className: string
  readonly testId: string
  readonly closeTestId?: string
  readonly onClose: () => void
  readonly releaseRemoteInput: () => void
  readonly initialFocus: string
  readonly children: ReactNode
  readonly footer: ReactNode
}

/** Non-modal local workspace: keep video usable, but never relay panel keys. */
export function ActionPanel(props: Props) {
  const root = useRef<HTMLElement>(null)
  const release = useRef(props.releaseRemoteInput)
  release.current = props.releaseRemoteInput

  useEffect(() => {
    if (!props.open || !root.current) return
    const panel = root.current
    const previous = document.activeElement
    release.current()
    panel.querySelector<HTMLElement>(props.initialFocus)?.focus()
    return () => {
      // Do not steal focus if the user has already clicked another toolbar or video.
      if (panel.contains(document.activeElement) || document.activeElement === document.body) {
        const fullscreen = document.fullscreenElement
        const destination = previous instanceof HTMLElement && previous.isConnected &&
          (!fullscreen || fullscreen.contains(previous))
          ? previous : document.querySelector<HTMLVideoElement>('video')
        destination?.focus()
      }
    }
  }, [props.open, props.initialFocus])

  if (!props.open) return null
  return (
    <aside ref={root} aria-label={props.title} className={`action-panel ${props.className}`}
      data-testid={props.testId} data-local-input
      onPointerDownCapture={() => release.current()}
      onKeyUp={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        event.stopPropagation()
        if (event.key === 'Escape' && !event.nativeEvent.isComposing) {
          event.preventDefault()
          props.onClose()
        }
      }}>
      <header className="action-panel-header">
        <div><strong>{props.title}</strong><span>{props.subtitle}</span></div>
        <IconButton icon="dismiss" label={`${props.title} 닫기`} data-testid={props.closeTestId}
          onClick={props.onClose} />
      </header>
      <div className="action-panel-body">{props.children}</div>
      <footer className="action-panel-footer">{props.footer}</footer>
    </aside>
  )
}
