import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageClipboardPanel } from '../src/ImageClipboardPanel'

const showModalDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
const closeDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close')

beforeEach(() => {
  // jsdom has no top-layer dialog implementation. Real focus/modal/fullscreen
  // behavior is also checked separately in an isolated Edge browser.
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true,
    value(this: HTMLDialogElement) { this.setAttribute('open', '') } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true,
    value(this: HTMLDialogElement) { this.removeAttribute('open') } })
  let id = 0
  vi.stubGlobal('URL', class extends URL {
    static override createObjectURL() { return `blob:test-image-${++id}` }
    static override revokeObjectURL() {}
  })
})
afterEach(() => {
  cleanup(); vi.unstubAllGlobals()
  if (showModalDescriptor) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', showModalDescriptor)
  else delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal
  if (closeDescriptor) Object.defineProperty(HTMLDialogElement.prototype, 'close', closeDescriptor)
  else delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close
})

const entry = (id: number) => ({ id, receivedAt: Date.now(), blob: new Blob(['test'], { type: 'image/png' }) })
function setup() {
  return { open: true, canSend: true, entries: [entry(1)], dismissEntry: vi.fn(),
    onClose: vi.fn(), releaseRemoteInput: vi.fn(), sendKey: vi.fn(), sendRemoteImage: vi.fn().mockResolvedValue(undefined) }
}

describe('received image thumbnail and explicit preview', () => {
  it('does not expand when the thumbnail or history row is clicked', () => {
    const props = setup()
    render(<ImageClipboardPanel {...props} />)
    fireEvent.click(screen.getByAltText('원격 PC에서 복사한 이미지'))
    fireEvent.click(screen.getByText('1 KB'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.querySelector('.image-preview-details')).toBeNull()
    expect(document.querySelector('.expanded-image-preview')).toBeNull()
    expect(props.sendKey).not.toHaveBeenCalled()
    expect(props.sendRemoteImage).not.toHaveBeenCalled()
  })

  it('opens only through an explicit button and restores focus after close', () => {
    render(<ImageClipboardPanel {...setup()} />)
    const trigger = screen.getByRole('button', { name: '확대 보기' })
    trigger.focus(); fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog')
    expect(screen.getByRole('dialog', { name: '받은 이미지 확대 보기' })).not.toBeNull()
    const close = screen.getByRole('button', { name: '이미지 확대 보기 닫기' })
    expect(document.activeElement).toBe(close)
    fireEvent.click(close)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(screen.getAllByAltText('원격 PC에서 복사한 이미지')).toHaveLength(1)
  })

  it('closes only the preview on Escape and never sends those keys remotely', () => {
    const props = setup()
    render(<ImageClipboardPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '확대 보기' }))
    const close = screen.getByRole('button', { name: '이미지 확대 보기 닫기' })
    fireEvent.keyDown(close, { key: 'v', code: 'KeyV', ctrlKey: true })
    fireEvent.keyUp(close, { key: 'v', code: 'KeyV', ctrlKey: true })
    fireEvent.keyDown(close, { key: 'Escape', code: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
    expect(props.onClose).not.toHaveBeenCalled()
    expect(props.sendKey).not.toHaveBeenCalled()
  })

  it('handles native cancel without closing the clipboard panel', () => {
    const props = setup()
    render(<ImageClipboardPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '확대 보기' }))
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(props.onClose).not.toHaveBeenCalled()
  })

  it('ignores a queued close from effect cleanup when the dialog has already reopened', () => {
    render(<ImageClipboardPanel {...setup()} />)
    fireEvent.click(screen.getByRole('button', { name: '확대 보기' }))
    const dialog = screen.getByRole('dialog') as HTMLDialogElement
    fireEvent(dialog, new Event('close'))
    expect(screen.getByRole('dialog')).toBe(dialog)
    dialog.removeAttribute('open')
    fireEvent(dialog, new Event('close'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps incoming rows compact when new images arrive without replacing an explicit preview', () => {
    const props = setup()
    const view = render(<ImageClipboardPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '확대 보기' }))
    const source = screen.getByAltText('받은 이미지 확대 미리보기').getAttribute('src')
    view.rerender(<ImageClipboardPanel {...props} entries={[entry(2), ...props.entries]} />)
    expect(screen.getAllByAltText('원격 PC에서 복사한 이미지')).toHaveLength(2)
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(screen.getByAltText('받은 이미지 확대 미리보기').getAttribute('src')).toBe(source)
    expect(document.querySelector('.expanded-image-preview')).toBeNull()
  })

  it('resets the preview on panel close or removal of its received image', () => {
    const props = setup()
    const view = render(<ImageClipboardPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '확대 보기' }))
    view.rerender(<ImageClipboardPanel {...props} open={false} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    view.rerender(<ImageClipboardPanel {...props} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '확대 보기' }))
    view.rerender(<ImageClipboardPanel {...props} entries={[]} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('image-clipboard-panel')).not.toBeNull()
  })
})
