import { useEffect, useRef, useState } from 'react'

import {
  type ClipboardImageEntry,
} from './useRemoteSession'

const IMAGE_MAX_BYTES = 20 * 1024 * 1024
const IMAGE_MAX_PIXELS = 40_000_000

interface ImageClipboardPanelProps {
  readonly canSend: boolean
  readonly entries: readonly ClipboardImageEntry[]
  readonly dismissEntry: (id: number) => void
  readonly onClose: () => void
  readonly sendKey: (code: string, action: 'down' | 'up') => void
  readonly sendRemoteImage: (image: Blob) => Promise<void>
}

function imageErrorMessage(error: unknown): string {
  const code = error instanceof Error ? error.message : ''
  switch (code) {
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

function IncomingImageCard({
  entry,
  dismiss,
  onStatus,
}: {
  readonly entry: ClipboardImageEntry
  readonly dismiss: () => void
  readonly onStatus: (message: string) => void
}) {
  const [previewUrl, setPreviewUrl] = useState('')

  useEffect(() => {
    const url = URL.createObjectURL(entry.blob)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [entry.blob])

  const saveImage = () => {
    const url = URL.createObjectURL(entry.blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `remote-capture-${new Date(entry.receivedAt)
      .toISOString()
      .replaceAll(':', '-')}.png`
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return (
    <li className="image-clipboard-card">
      {previewUrl && (
        <img alt="원격 PC에서 복사한 이미지" src={previewUrl} />
      )}
      <div className="image-clipboard-card-actions">
        <button
          onClick={() => {
            void copyImageToLocalClipboard(entry.blob)
              .then(() => onStatus('이 컴퓨터의 클립보드에 복사했습니다. Ctrl+V로 붙여넣으세요.'))
              .catch((error: unknown) => onStatus(imageErrorMessage(error)))
          }}
          type="button"
        >
          이 컴퓨터에 복사
        </button>
        <button onClick={saveImage} type="button">
          저장
        </button>
        <button aria-label="이미지 삭제" onClick={dismiss} type="button">
          ×
        </button>
      </div>
    </li>
  )
}

export function ImageClipboardPanel({
  canSend,
  entries,
  dismissEntry,
  onClose,
  sendKey,
  sendRemoteImage,
}: ImageClipboardPanelProps) {
  const [selectedImage, setSelectedImage] = useState<Blob | null>(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [status, setStatus] = useState(
    '원격 PC에서 이미지를 복사하거나 캡처하면 아래에 자동으로 나타납니다.',
  )
  const [sending, setSending] = useState(false)
  const pasteTarget = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!selectedImage) {
      setPreviewUrl('')
      return
    }
    const url = URL.createObjectURL(selectedImage)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [selectedImage])

  const acceptImage = async (image: Blob) => {
    try {
      const png = await normalizeClipboardImage(image)
      setSelectedImage(png)
      setStatus('이미지를 불러왔습니다. 원격 PC에 붙여넣기를 눌러주세요.')
    } catch (error) {
      setStatus(imageErrorMessage(error))
    }
  }

  const pasteToRemote = async () => {
    if (!selectedImage || sending) {
      return
    }
    setSending(true)
    setStatus('원격 PC의 클립보드로 전송 중입니다…')
    try {
      await sendRemoteImage(selectedImage)
      sendRemotePasteShortcut(sendKey)
      setStatus('원격 PC에 붙여넣었습니다.')
    } catch (error) {
      setStatus(imageErrorMessage(error))
    } finally {
      setSending(false)
    }
  }

  return (
    <aside
      aria-label="양방향 이미지 클립보드"
      className="image-clipboard-panel"
      data-testid="image-clipboard-panel"
    >
      <header className="image-clipboard-header">
        <div>
          <strong>이미지 클립보드</strong>
          <span>접속 PC ↔ 원격 PC</span>
        </div>
        <button aria-label="이미지 클립보드 닫기" onClick={onClose} type="button">
          ×
        </button>
      </header>

      <section className="image-clipboard-send">
        <h2>원격 PC에 붙여넣기</h2>
        <div
          className="image-paste-target"
          data-testid="image-paste-target"
          onClick={() => pasteTarget.current?.focus()}
          onPaste={(event) => {
            const item = Array.from(event.clipboardData.items).find((candidate) =>
              candidate.type.startsWith('image/'),
            )
            const image = item?.getAsFile()
            if (image) {
              event.preventDefault()
              void acceptImage(image)
            } else {
              setStatus('클립보드에 이미지가 없습니다.')
            }
          }}
          ref={pasteTarget}
          role="textbox"
          tabIndex={0}
        >
          {previewUrl ? (
            <img alt="원격 PC로 보낼 이미지 미리보기" src={previewUrl} />
          ) : (
            <span>여기를 클릭하고 Ctrl+V로 이미지를 붙여넣으세요.</span>
          )}
        </div>
        <div className="image-clipboard-actions">
          <button
            disabled={!canSend || sending}
            onClick={() => {
              void readImageFromLocalClipboard()
                .then(acceptImage)
                .catch((error: unknown) => setStatus(imageErrorMessage(error)))
            }}
            type="button"
          >
            클립보드 이미지 불러오기
          </button>
          <button
            className="image-paste-primary"
            data-testid="image-paste-remote"
            disabled={!canSend || !selectedImage || sending}
            onClick={() => void pasteToRemote()}
            type="button"
          >
            {sending ? '전송 중…' : '원격 PC에 붙여넣기'}
          </button>
        </div>
      </section>

      <section className="image-clipboard-receive">
        <h2>원격 PC에서 가져오기</h2>
        {entries.length === 0 ? (
          <p>원격 PC에서 이미지를 복사하거나 캡처한 뒤 잠시 기다려주세요.</p>
        ) : (
          <ul>
            {entries.map((entry) => (
              <IncomingImageCard
                dismiss={() => dismissEntry(entry.id)}
                entry={entry}
                key={entry.id}
                onStatus={setStatus}
              />
            ))}
          </ul>
        )}
      </section>

      <p className="image-clipboard-status" role="status">
        {status}
      </p>
    </aside>
  )
}
