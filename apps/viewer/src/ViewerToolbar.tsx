import { useEffect, useRef, useState } from 'react'
import { IconButton } from './IconButton'

type Profile = 'low' | 'balanced' | 'high'
interface Props {
  readonly fullscreen?: boolean
  readonly videoProfile: Profile
  readonly videoProfilePending: boolean
  readonly videoProfileStabilizing: boolean
  readonly videoProfileError: string | null
  readonly canDisplay: boolean
  readonly hasVideo: boolean
  readonly actualSize: boolean
  readonly isControlActive: boolean
  readonly canSendClipboardImage: boolean
  readonly canSendFiles: boolean
  readonly textOpen: boolean
  readonly imageOpen: boolean
  readonly imageUnread?: boolean
  readonly downloadsOpen: boolean
  readonly setVideoProfile: (value: Profile) => void
  readonly onDisplayMode: () => void
  readonly onFullscreen: () => void
  readonly onCapture: () => void
  readonly onText: () => void
  readonly onImage: () => void
  readonly onUpload: () => void
  readonly onDownload: () => void
  readonly onBusyChange?: (busy: boolean) => void
}

export function ViewerToolbar(props: Props) {
  const [qualityOpen, setQualityOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const toolbar = useRef<HTMLDivElement>(null)
  const prefix = props.fullscreen ? 'fullscreen-' : ''
  const id = `${prefix}quality-popover`

  useEffect(() => {
    props.onBusyChange?.(qualityOpen || moreOpen)
  }, [qualityOpen, moreOpen, props.onBusyChange])

  useEffect(() => {
    if (!qualityOpen) return
    const popover = toolbar.current?.querySelector<HTMLElement>('.quality-popover')
    popover?.querySelector<HTMLInputElement>('input:checked:not(:disabled)')?.focus()
    return () => {
      if (popover?.contains(document.activeElement)) {
        toolbar.current?.querySelector<HTMLButtonElement>('[data-quality-toggle]')?.focus()
      }
    }
  }, [qualityOpen])

  useEffect(() => {
    if (!qualityOpen && !moreOpen) return
    const close = (event: PointerEvent) => {
      if (event.target instanceof Node && !toolbar.current?.contains(event.target)) {
        setQualityOpen(false)
        setMoreOpen(false)
      }
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [qualityOpen, moreOpen])

  const finishAction = (action: () => void) => {
    setMoreOpen(false)
    action()
  }

  const captureButton = <IconButton icon="capture" label="캡처" title="원격 PC 화면 캡처 (Win+Shift+S)"
    data-testid={`${prefix}capture-button`} disabled={!props.isControlActive} onClick={props.onCapture} />

  return (
    <div ref={toolbar} className={`viewer-toolbar ${props.fullscreen ? 'palette-toolbar' : ''}`}
      role="toolbar" aria-label={props.fullscreen ? '전체화면 메뉴' : '화면 표시 설정'}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && (qualityOpen || moreOpen)) {
          event.preventDefault()
          event.stopPropagation()
          setQualityOpen(false)
          setMoreOpen(false)
          toolbar.current?.querySelector<HTMLButtonElement>('[data-quality-toggle]')?.focus()
        }
      }}>
      <div className="toolbar-group display-actions">
        <div className="quality-menu">
          <IconButton icon="quality" label="화질" data-quality-toggle
            data-testid={`${prefix}quality-button`} aria-expanded={qualityOpen} aria-controls={id}
            aria-pressed={qualityOpen} onClick={() => { setQualityOpen(!qualityOpen); setMoreOpen(false) }} />
          <div className="quality-popover" id={id} hidden={!qualityOpen}>
            <header className="quality-popover-header">
              <div><strong>화질</strong><span>해상도 · FPS 고정</span></div>
              <IconButton icon="dismiss" label="화질 창 닫기" onClick={() => setQualityOpen(false)} />
            </header>
            <fieldset className="quality-choices" data-testid={`${prefix}video-profile-select`}
              disabled={props.videoProfilePending || props.videoProfileStabilizing}>
              <legend className="sr-only">{props.fullscreen ? '전체화면 영상 화질' : '영상 화질'}</legend>
              {([
                ['low', 'Low', '1280×720 고정 / 10fps'],
                ['balanced', 'Balanced', '1600×900 고정 / 15fps'],
                ['high', 'High', '1920×1080 고정 / 20fps'],
              ] as const).map(([value, label, detail]) => (
                <label className="quality-choice" key={value} data-selected={props.videoProfile === value}>
                  <input type="radio" name={`${prefix}video-profile`} value={value}
                    data-testid={`${prefix}video-profile-${value}`} checked={props.videoProfile === value}
                    onClick={() => { if (props.videoProfile === value) setQualityOpen(false) }}
                    onChange={() => { props.setVideoProfile(value); setQualityOpen(false) }} />
                  <span><strong>{label}</strong><small>{detail}</small></span>
                  <span className="quality-choice-check" aria-hidden="true">{props.videoProfile === value ? '✓' : ''}</span>
                </label>
              ))}
            </fieldset>
          </div>
        </div>
        <IconButton icon={props.actualSize ? 'actual' : 'fit'} label={props.actualSize ? '실제 크기' : '화면 맞춤'}
          title={props.actualSize ? '화면 맞춤으로 전환' : '실제 크기로 전환'}
          data-testid={`${prefix}display-mode-button`} disabled={!props.canDisplay}
          aria-pressed={props.actualSize} onClick={props.onDisplayMode} />
        {!props.fullscreen && <IconButton icon="fullscreen" label="전체화면"
          data-testid="fullscreen-button" disabled={!props.hasVideo} onClick={props.onFullscreen} />}
        {props.fullscreen && <>
          <IconButton icon="exit-fullscreen" label="전체화면 종료"
            data-testid="fullscreen-exit-button" onClick={props.onFullscreen} />
          {captureButton}
        </>}
      </div>
      <div className="toolbar-group capture-actions">
        {!props.fullscreen && captureButton}
        <IconButton icon="text" label="텍스트 클립보드"
          title="접속 PC와 원격 PC 사이에서 텍스트를 복사하고 붙여넣기"
          data-testid={`${prefix}clipboard-button`} disabled={!props.isControlActive}
          data-panel-open={props.textOpen}
          aria-pressed={props.textOpen} onClick={props.onText} />
        <IconButton icon="image" label="이미지 클립보드"
          title={props.imageUnread ? '새 이미지 도착 · 이미지 클립보드 열기' : '접속 PC와 원격 PC 사이에서 이미지를 복사하고 붙여넣기'}
          data-unread={props.imageUnread || undefined}
          data-panel-open={props.imageOpen}
          data-testid={`${prefix}image-clipboard-button`}
          disabled={!props.isControlActive || !props.canSendClipboardImage}
          aria-pressed={props.imageOpen} onClick={() => finishAction(props.onImage)} />
      </div>
      <div className="toolbar-group secondary-actions" data-expanded={moreOpen}>
        <IconButton icon="upload" label="파일 전송" title="집 PC로 파일 보내기"
          data-testid={`${prefix}file-send-button`} disabled={!props.canSendFiles}
          onClick={() => finishAction(props.onUpload)} />
        <IconButton icon="download" label="파일 받기" title="집 PC에서 파일 받기"
          data-testid={`${prefix}file-receive-button`} disabled={!props.canSendFiles}
          aria-pressed={props.downloadsOpen} onClick={() => finishAction(props.onDownload)} />
      </div>
      {!props.fullscreen && <span className="toolbar-more">
        <IconButton icon="more" label="더보기" data-testid="toolbar-more-button" aria-expanded={moreOpen}
          onClick={() => { setMoreOpen(!moreOpen); setQualityOpen(false) }} />
      </span>}
      <span className="toolbar-feedback" aria-live="polite">
        {(props.videoProfilePending || props.videoProfileStabilizing) && <span className="quality-pending" role="status">
          {props.videoProfilePending ? '적용 중…' : '영상 안정화 중…'}
        </span>}
        {props.videoProfileError && <span className="quality-error" role="alert">{props.videoProfileError}</span>}
      </span>
    </div>
  )
}
