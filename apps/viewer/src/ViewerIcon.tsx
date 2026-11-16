export type ViewerIconName =
  | 'monitor' | 'quality' | 'fit' | 'actual' | 'fullscreen' | 'exit-fullscreen'
  | 'capture' | 'text' | 'image' | 'upload' | 'download' | 'tools' | 'more'
  | 'pin' | 'close' | 'dismiss' | 'disconnect' | 'connect' | 'stats'

export function ViewerIcon({ name }: { readonly name: ViewerIconName }) {
  const isConnectionIcon = name === 'connect' || name === 'disconnect'
  return (
    <svg className="viewer-icon" viewBox={isConnectionIcon ? '0 0 26 24' : '0 0 24 24'} fill="none" stroke="currentColor"
      strokeWidth={isConnectionIcon ? 2 : 1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {name === 'monitor' && <><rect x="3" y="3" width="18" height="13" rx="2" /><path d="M8 21h8m-4-5v5" /></>}
      {name === 'quality' && <><path d="M3 6h4m4 0h10M3 12h10m4 0h4M3 18h4m4 0h10" /><circle cx="9" cy="6" r="2" /><circle cx="15" cy="12" r="2" /><circle cx="9" cy="18" r="2" /></>}
      {name === 'fit' && <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m9 9-3 3 3 3m6-6 3 3-3 3M6 12h12" /></>}
      {name === 'actual' && <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M8 10v5m6-5v5" /><circle cx="11" cy="11" r=".3" /><circle cx="11" cy="14" r=".3" /></>}
      {name === 'fullscreen' && <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" />}
      {name === 'exit-fullscreen' && <path d="M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5" />}
      {name === 'capture' && <>
        <path d="M4 9V3h6m4 0h7v6m0 4v4h-5" strokeDasharray="2.5 2.5" />
        <circle cx="4.5" cy="16" r="2.5" /><circle cx="10.5" cy="20" r="2.5" />
        <path d="m6.4 14.4 8.1-7.9M8.6 18.4l-2.2-2m-.2-.3 8.3 3.4" />
      </>}
      {name === 'text' && <><rect x="5" y="4" width="14" height="17" rx="2" /><rect x="9" y="2" width="6" height="4" rx="1" /><path d="M9 10h6m-3 0v7" /></>}
      {name === 'image' && <><rect x="4" y="4" width="16" height="17" rx="2" /><path d="M9 4V2h6v2M7 17l4-4 3 3 2-2 2 3" /><circle cx="15.5" cy="9.5" r="1.5" /></>}
      {name === 'upload' && <><path d="M4 15v5h16v-5M12 16V3m-5 5 5-5 5 5" /></>}
      {name === 'download' && <><path d="M4 15v5h16v-5M12 3v13m-5-5 5 5 5-5" /></>}
      {name === 'tools' && <><rect x="4" y="4" width="5" height="5" rx="1" /><rect x="15" y="4" width="5" height="5" rx="1" /><rect x="4" y="15" width="5" height="5" rx="1" /><rect x="15" y="15" width="5" height="5" rx="1" /></>}
      {name === 'more' && <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>}
      {name === 'pin' && <>
        <path d="M12 17v5" />
        <path className="viewer-pin-body" d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />
      </>}
      {name === 'close' && <path d="m9 5 7 7-7 7" />}
      {name === 'dismiss' && <path d="m6 6 12 12M18 6 6 18" />}
      {isConnectionIcon && <>
        <path d="M21 5V4a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-1M8 22h8m-4-4v4" />
        <path d={name === 'connect' ? 'M24 10H12m5-5-5 5 5 5' : 'M12 10h12m-5-5 5 5-5 5'} />
      </>}
      {name === 'stats' && <><path d="M4 20V4m0 16h17M8 15l4-5 4 3 5-7" /></>}
    </svg>
  )
}
