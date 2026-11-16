import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ActionPanel } from './ActionPanel'
import { IconButton } from './IconButton'

import {
  type ClipboardImageEntry,
} from './useRemoteSession'

const IMAGE_MAX_BYTES = 20 * 1024 * 1024
const IMAGE_MAX_PIXELS = 40_000_000

interface ImageClipboardPanelProps {
  readonly open: boolean
  readonly releaseRemoteInput: () => void
  readonly canSend: boolean
  readonly entries: readonly ClipboardImageEntry[]
  readonly dismissEntry: (id: number) => void
  readonly onClose: () => void
  readonly sendKey: (code: string, action: 'down' | 'up') => void
  readonly sendRemoteImage: (image: Blob) => Promise<void>
}

function imageErrorMessage(error: unknown): string {
  const code = error instanceof Error ? (error.name === 'NotAllowedError' ? error.name : error.message) : ''
  switch (code) {
    case 'CLIPBOARD_UNAVAILABLE':
      return '브라우저가 클립보드 접근을 지원하지 않습니다. Ctrl+V로 불러오거나 저장을 이용해 주세요.'
    case 'NotAllowedError':
      return '클립보드 접근이 차단되었습니다. 브라우저 권한을 허용하거나 Ctrl+V로 불러와 주세요.'
    case 'CONTROL_INACTIVE':
      return '원격 제어가 활성화되어 있지 않습니다.'
    case 'BUSY':
      return '다른 이미지가 전송 중입니다.'
    case 'TIMEOUT':
      return '원격 PC의 응답 시간이 초과되었습니다.'
    case 'INVALID_IMAGE':
      return '지원하지 않거나 너무 큰 이미지입니다.'
    default:
      return '이미지를 처리하지 못했습니다. 다시 복사해 주세요.'
  }
}

async function canvasToPng(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) {
        resolve(blob)
      } else {
        reject(new Error('INVALID_IMAGE'))
      }
    }, 'image/png')
  })
}

export async function normalizeClipboardImage(image: Blob): Promise<Blob> {
  if (!image.type.startsWith('image/')) {
    throw new Error('INVALID_IMAGE')
  }
  if (image.type === 'image/png' && image.size <= IMAGE_MAX_BYTES) {
    return image
  }
  const bitmap = await createImageBitmap(image)
  try {
    if (
      bitmap.width <= 0 ||
      bitmap.height <= 0 ||
      bitmap.width * bitmap.height > IMAGE_MAX_PIXELS
    ) {
      throw new Error('INVALID_IMAGE')
    }
    const canvas = document.createElement('canvas')
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const context = canvas.getContext('2d')
    if (!context) {
      throw new Error('INVALID_IMAGE')
    }
    context.drawImage(bitmap, 0, 0)
    const png = await canvasToPng(canvas)
    if (png.size > IMAGE_MAX_BYTES) {
      throw new Error('INVALID_IMAGE')
    }
    return png
  } finally {
    bitmap.close()
  }
}

async function readImageFromLocalClipboard(): Promise<Blob> {
  if (!window.isSecureContext || !navigator.clipboard?.read) {
    throw new Error('CLIPBOARD_UNAVAILABLE')
  }
  const items = await navigator.clipboard.read()
  for (const item of items) {
    const imageType = item.types.find((type) => type.startsWith('image/'))
    if (imageType) {
      return normalizeClipboardImage(await item.getType(imageType))
    }
  }
  throw new Error('INVALID_IMAGE')
}

async function copyImageToLocalClipboard(image: Blob): Promise<void> {
  if (
    !window.isSecureContext ||
    !navigator.clipboard?.write ||
    typeof ClipboardItem === 'undefined'
  ) {
    throw new Error('CLIPBOARD_UNAVAILABLE')
  }
  // Host->viewer payloads are already normalized PNGs. Avoid an await before
  // clipboard.write in that common path so Chromium's user activation from the
  // click is unquestionably preserved.
  let png = image
  if (image.type !== 'image/png' || image.size > IMAGE_MAX_BYTES) {
    png = await normalizeClipboardImage(image)
  }
  await navigator.clipboard.write([
    new ClipboardItem({
      'image/png': png,
    }),
  ])
}

function sendRemotePasteShortcut(
  sendKey: (code: string, action: 'down' | 'up') => void,
): void {
  sendKey('ControlLeft', 'down')
  sendKey('KeyV', 'down')
  sendKey('KeyV', 'up')
  sendKey('ControlLeft', 'up')
}

type StatusTone = 'info' | 'success' | 'error'
type ReportStatus = (message: string, tone?: StatusTone) => void

/** Top-layer preview: does not resize history rows and stays inside fullscreen. */
function ImagePreviewDialog({ url, onClose }: {
  readonly url: string
  readonly onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useLayoutEffect(() => {
    const modal = dialog.current
    if (!modal) return
    const previous = document.activeElement
    modal.showModal()
    modal.querySelector<HTMLButtonElement>('button')?.focus()
    return () => {
      const hadFocus = modal.contains(document.activeElement)
      modal.close()
      if (hadFocus && previous instanceof HTMLElement && previous.isConnected) previous.focus()
    }
  }, [])
  return (
    <dialog ref={dialog} className="image-preview-dialog" aria-labelledby={titleId}
      data-testid="received-image-preview" data-local-input
      onCancel={(event) => { event.preventDefault(); onClose() }}
      onClose={(event) => {
        // Ignore a queued close event if StrictMode cleanup already reopened it.
        if (!event.currentTarget.open) onClose()
      }}
      onKeyUp={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        event.stopPropagation()
        if (event.key === 'Escape' && !event.nativeEvent.isComposing) {
          event.preventDefault()
          onClose()
        } else if (event.key === 'Tab') {
          // The preview has one action; keep focus on it rather than browser chrome.
          event.preventDefault()
          event.currentTarget.querySelector<HTMLButtonElement>('button')?.focus()
        }
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return
        const bounds = event.currentTarget.getBoundingClientRect()
        if (event.clientX < bounds.left || event.clientX > bounds.right ||
            event.clientY < bounds.top || event.clientY > bounds.bottom) onClose()
      }}>
      <header className="image-preview-header">
        <strong id={titleId}>받은 이미지 확대 보기</strong>
        <IconButton icon="dismiss" label="이미지 확대 보기 닫기" onClick={onClose} />
      </header>
      <div className="image-preview-body">
        <img className="image-preview-large" alt="받은 이미지 확대 미리보기" src={url} decoding="async" />
      </div>
    </dialog>
  )
}

function IncomingImageCard({ entry, dismiss, onStatus }: {
  readonly entry: ClipboardImageEntry
  readonly dismiss: () => void
  readonly onStatus: ReportStatus
}) {
  const [previewUrl, setPreviewUrl] = useState('')
  const [copying, setCopying] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  useEffect(() => {
    const url = URL.createObjectURL(entry.blob)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [entry.blob])
  const saveImage = () => {
    const url = URL.createObjectURL(entry.blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `remote-capture-${new Date(entry.receivedAt).toISOString().replaceAll(':', '-')}.png`
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
    onStatus('이미지 저장을 요청했습니다.')
  }
  return (
    <li className="image-clipboard-card compact-image-card">
      <div className="incoming-image-summary">
        {previewUrl && <img className="incoming-image-thumbnail" alt="원격 PC에서 복사한 이미지" src={previewUrl} decoding="async" />}
        <span>{Math.max(1, Math.round(entry.blob.size / 1024))} KB</span>
        <button type="button" aria-haspopup="dialog" disabled={!previewUrl}
          onClick={() => setPreviewOpen(true)}>확대 보기</button>
      </div>
      {previewOpen && previewUrl && <ImagePreviewDialog url={previewUrl} onClose={() => setPreviewOpen(false)} />}
      <div className="image-clipboard-card-actions">
        <button disabled={copying} onClick={() => {
          setCopying(true)
          void copyImageToLocalClipboard(entry.blob)
            .then(() => onStatus('이 컴퓨터의 클립보드에 복사했습니다. Ctrl+V로 붙여넣으세요.', 'success'))
            .catch((error: unknown) => onStatus(imageErrorMessage(error), 'error'))
            .finally(() => setCopying(false))
        }} type="button">{copying ? '복사 중…' : '이 컴퓨터에 복사'}</button>
        <button onClick={saveImage} type="button">저장</button>
        <button aria-label="이미지 삭제" onClick={dismiss} type="button">×</button>
      </div>
    </li>
  )
}

export function ImageClipboardPanel(props: ImageClipboardPanelProps) {
  const [selectedImage, setSelectedImage] = useState<Blob | null>(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [status, setStatus] = useState('이미지를 복사한 뒤 Ctrl+V로 불러오세요.')
  const [tone, setTone] = useState<StatusTone>('info')
  const [sending, setSending] = useState(false)
  const [processing, setProcessing] = useState(false)
  const sendInFlight = useRef(false)
  const imageJob = useRef(0)
  const pasteTarget = useRef<HTMLDivElement>(null)
  const reportStatus: ReportStatus = (message, kind = 'info') => {
    setStatus(message)
    setTone(kind)
  }

  useEffect(() => {
    if (!selectedImage || !props.open) {
      setPreviewUrl('')
      return
    }
    const url = URL.createObjectURL(selectedImage)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [selectedImage, props.open])

  const acceptImage = async (image: Blob) => {
    if (sendInFlight.current) return
    const job = ++imageJob.current
    setProcessing(true)
    try {
      const png = await normalizeClipboardImage(image)
      if (job !== imageJob.current) return
      setSelectedImage(png)
      reportStatus('이미지를 불러왔습니다. 원격 PC에 붙여넣기를 눌러주세요.')
    } catch (error) {
      if (job === imageJob.current) reportStatus(imageErrorMessage(error), 'error')
    } finally {
      if (job === imageJob.current) setProcessing(false)
    }
  }
  const pasteToRemote = async () => {
    if (!selectedImage || sendInFlight.current || processing) return
    sendInFlight.current = true
    setSending(true)
    reportStatus('원격 PC의 클립보드로 전송 중입니다…')
    try {
      await props.sendRemoteImage(selectedImage)
      sendRemotePasteShortcut(props.sendKey)
      reportStatus('원격 클립보드 전송 완료 · 붙여넣기 요청을 보냈습니다.', 'success')
    } catch (error) {
      reportStatus(imageErrorMessage(error), 'error')
    } finally {
      sendInFlight.current = false
      setSending(false)
    }
  }

  return (
    <ActionPanel open={props.open} title="이미지 클립보드" subtitle="접속 PC ↔ 원격 PC"
      className="image-clipboard-panel" testId="image-clipboard-panel"
      onClose={props.onClose} releaseRemoteInput={props.releaseRemoteInput}
      initialFocus='[role="textbox"]'
      footer={<>
        <div className="action-panel-actions">
          <button disabled={!props.canSend || sending || processing} type="button"
            onClick={() => {
              void readImageFromLocalClipboard().then(acceptImage)
                .catch((error: unknown) => reportStatus(imageErrorMessage(error), 'error'))
            }}>이미지 불러오기</button>
          <button className="image-paste-primary" data-testid="image-paste-remote"
            disabled={!props.canSend || !selectedImage || sending || processing}
            onClick={() => void pasteToRemote()} type="button">
            {sending ? '전송 중…' : processing ? '처리 중…' : '원격 PC에 붙여넣기'}
          </button>
        </div>
        <p className="action-panel-status" data-tone={tone} role="status">{status}</p>
      </>}>
      <section className="image-clipboard-send">
        <h2>접속 PC → 원격 PC</h2>
        <div className="image-paste-target" data-testid="image-paste-target"
          aria-label="원격 PC로 보낼 이미지 붙여넣기 영역" aria-multiline="false"
          onClick={() => pasteTarget.current?.focus()}
          onPaste={(event) => {
            event.preventDefault()
            if (sending || processing) return
            const item = Array.from(event.clipboardData.items).find(candidate => candidate.type.startsWith('image/'))
            const image = item?.getAsFile()
            if (image) void acceptImage(image)
            else reportStatus('클립보드에 이미지가 없습니다.', 'error')
          }} ref={pasteTarget} role="textbox" tabIndex={0}>
          {previewUrl ? <img alt="원격 PC로 보낼 이미지 미리보기" src={previewUrl} decoding="async" /> :
            <span>Ctrl+V로 이미지를 붙여넣으세요.</span>}
        </div>
        {selectedImage && <button className="image-draft-clear" type="button" disabled={sending || processing}
          onClick={() => { setSelectedImage(null); reportStatus('선택한 이미지를 지웠습니다.') }}>선택 이미지 지우기</button>}
      </section>
      <section className="image-clipboard-receive">
        <h2>원격 PC → 접속 PC · 받은 이미지 {props.entries.length}개</h2>
        {props.entries.length === 0 ? <p>원격 PC에서 복사하거나 캡처한 이미지가 여기에 표시됩니다.</p> :
          <ul>{props.entries.map(entry => (
            <IncomingImageCard dismiss={() => props.dismissEntry(entry.id)} entry={entry} key={entry.id} onStatus={reportStatus} />
          ))}</ul>}
      </section>
    </ActionPanel>
  )
}
