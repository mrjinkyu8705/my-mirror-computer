import {cleanup, render, screen} from '@testing-library/react'
import {afterEach, describe, expect, it} from 'vitest'
import {ViewerIcon} from '../src/ViewerIcon'
import {IconButton} from '../src/IconButton'

afterEach(cleanup)

describe('connection icons', () => {
  it.each([
    ['connect', 'M24 10H12m5-5-5 5 5 5'],
    ['disconnect', 'M12 10h12m-5-5 5 5-5 5'],
  ] as const)('renders %s with a monitor and its directional arrow', (name, arrow) => {
    const {container} = render(<ViewerIcon name={name} />)
    const svg = container.querySelector('svg')!
    expect(svg.getAttribute('viewBox')).toBe('0 0 26 24')
    expect(svg.getAttribute('stroke-width')).toBe('2')
    expect(svg.getAttribute('aria-hidden')).toBe('true')
    expect(svg.getAttribute('focusable')).toBe('false')
    const paths = svg.querySelectorAll('path')
    expect(paths).toHaveLength(2)
    expect(paths[0]!.getAttribute('d')).toContain('M8 22h8m-4-4v4')
    expect(paths[1]!.getAttribute('d')).toBe(arrow)
  })

  it('does not change the viewport or line weight of other icons', () => {
    const {container} = render(<ViewerIcon name="fullscreen" />)
    expect(container.querySelector('svg')!.getAttribute('viewBox')).toBe('0 0 24 24')
    expect(container.querySelector('svg')!.getAttribute('stroke-width')).toBe('1.7')
  })

  it('keeps connection button names accessible and icons decorative', () => {
    render(<><IconButton icon="connect" label="원격 연결" /><IconButton icon="disconnect" label="연결 종료" /></>)
    expect(screen.getByRole('button', {name:'원격 연결'}).querySelector('svg')!.getAttribute('aria-hidden')).toBe('true')
    expect(screen.getByRole('button', {name:'연결 종료'}).querySelector('svg')!.getAttribute('aria-hidden')).toBe('true')
  })
})

describe('upright pin icon', () => {
  it('uses a centered vertical needle and a separate closed pin body', () => {
    const {container} = render(<ViewerIcon name="pin" />)
    const svg = container.querySelector('svg')!
    expect(svg.getAttribute('viewBox')).toBe('0 0 24 24')
    expect(svg.getAttribute('fill')).toBe('none')
    expect(svg.getAttribute('stroke-width')).toBe('1.7')
    expect(svg.querySelectorAll('path')).toHaveLength(2)
    expect(svg.querySelector('path')!.getAttribute('d')).toBe('M12 17v5')
    expect(svg.querySelector('.viewer-pin-body')!.getAttribute('d')).toMatch(/z$/)
    expect(svg.getAttribute('aria-hidden')).toBe('true')
  })

  it('keeps pressed state accessible without changing the geometry or button identity', () => {
    const {rerender} = render(<IconButton icon="pin" label="도구함 고정" aria-pressed={false} />)
    const button = screen.getByRole('button', {name:'도구함 고정'})
    const body = button.querySelector('.viewer-pin-body')!
    const geometry = body.getAttribute('d')
    expect(button.getAttribute('aria-pressed')).toBe('false')
    rerender(<IconButton icon="pin" label="도구함 고정" aria-pressed />)
    expect(screen.getByRole('button', {name:'도구함 고정'})).toBe(button)
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(button.querySelector('.viewer-pin-body')).toBe(body)
    expect(body.getAttribute('d')).toBe(geometry)
  })

  it('does not add pin fill targets to other toolbar icons', () => {
    const {container} = render(<IconButton icon="fullscreen" label="전체화면" aria-pressed />)
    expect(container.querySelector('.viewer-pin-body')).toBeNull()
    expect(container.querySelector('svg')!.getAttribute('fill')).toBe('none')
  })
})
