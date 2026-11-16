import type { ButtonHTMLAttributes } from 'react'
import { ViewerIcon, type ViewerIconName } from './ViewerIcon'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  readonly icon: ViewerIconName
  readonly label: string
}

export function IconButton({ icon, label, title, className = '', ...props }: Props) {
  return (
    <span className="icon-button-wrap" data-tooltip={title ?? label}>
      <button {...props} type="button" className={`icon-button ${className}`}
        aria-label={label} title={title ?? label}>
        <ViewerIcon name={icon} />
        <span className="sr-only">{label}</span>
      </button>
    </span>
  )
}
