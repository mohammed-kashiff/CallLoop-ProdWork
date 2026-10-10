import type { ReactNode } from 'react'

export function EmptyState({
  title,
  copy,
  lead,
  children,
  className,
}: {
  title: string
  copy: string
  lead?: ReactNode
  children?: ReactNode
  className?: string
}) {
  return (
    <div className={['empty-card', 'is-idle', className].filter(Boolean).join(' ')}>
      {lead}
      <p className="empty-title">{title}</p>
      <p className="empty-copy">{copy}</p>
      {children}
    </div>
  )
}
