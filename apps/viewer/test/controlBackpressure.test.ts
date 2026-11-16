import { describe, expect, it } from 'vitest'
import { CONTROL_BUFFER_HIGH, PendingPointerMove } from '../src/controlBackpressure'

describe('pointer backpressure', () => {
  it('sends immediately on a healthy channel and keeps only the latest blocked position', () => {
    const moves = new PendingPointerMove()
    expect(moves.offer(0, 0, 0, 0)).toBe(true)
    expect(moves.offer(0.1, 0.2, CONTROL_BUFFER_HIGH, 10)).toBe(false)
    moves.offer(0.8, 0.9, CONTROL_BUFFER_HIGH, 20)
    expect(moves.take(30)).toEqual({ x: 0.8, y: 0.9 })
    expect(moves.take(31)).toBeNull()
  })
  it('drops stale moves on drain but retains coordinates before clicks/releases', () => {
    const moves = new PendingPointerMove()
    moves.offer(0.8, 0.9, CONTROL_BUFFER_HIGH, 0)
    expect(moves.take(201)).toBeNull()
    moves.offer(0.3, 0.4, CONTROL_BUFFER_HIGH, 0)
    expect(moves.take(250, true)).toEqual({ x: 0.3, y: 0.4 })
  })
  it('does not replay positions after blur/disconnect or a newer immediate position', () => {
    const moves = new PendingPointerMove()
    moves.offer(0.8, 0.9, CONTROL_BUFFER_HIGH, 0)
    moves.clear()
    expect(moves.take(1, true)).toBeNull()
    moves.offer(0.1, 0.2, CONTROL_BUFFER_HIGH, 0)
    expect(moves.offer(1, 1, 0, 10)).toBe(true)
    expect(moves.take(11)).toBeNull()
  })
})
