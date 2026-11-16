/** Opt-in, URL-scoped trial. No storage, global defaults or arbitrary servers. */
export function diagnosticIceConfiguration(
  iceServers: RTCIceServer[], search: string,
): RTCConfiguration {
  const params = new URLSearchParams(search)
  if (!params.has('diagnosticTurn')) return { iceServers }
  const mode = params.get('diagnosticTurn')
  if (!['udp', 'tcp', 'tls'].includes(mode ?? '') || params.getAll('diagnosticTurn').length !== 1) {
    throw new Error('진단 전송 방식이 올바르지 않습니다. 주소에서 diagnosticTurn 항목을 제거해 주세요.')
  }
  const selected: RTCIceServer[] = []
  for (const server of iceServers) {
    const urls = typeof server.urls === 'string' ? [server.urls] : server.urls
    const matching = urls.filter((url) => {
      const match = /^(turns?):[^?]+(?:\?(.*))?$/.exec(url)
      if (!match) return false
      const transport = new URLSearchParams(match[2] ?? '').get('transport')
        ?? (match[1] === 'turns' ? 'tcp' : 'udp')
      return mode === 'tls' ? match[1] === 'turns' && transport === 'tcp'
        : match[1] === 'turn' && transport === mode
    })
    if (matching.length) selected.push({ ...server, urls: matching })
  }
  if (!selected.length) {
    // Never fall back silently and mislabel an unsuccessful experiment.
    throw new Error('요청한 TURN 진단 경로가 제공되지 않습니다. 일반 주소로 다시 접속해 주세요.')
  }
  return { iceServers: selected, iceTransportPolicy: 'relay' }
}
