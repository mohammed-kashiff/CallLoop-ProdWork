import type { ReactNode } from 'react'

export function CommandCenterPage({
  title,
  crumb,
  actions,
  children,
}: {
  title: string
  crumb?: ReactNode
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="mx-auto max-w-[1120px] px-1 pb-16">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          {crumb ? (
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-cc-muted">
              {crumb}
            </p>
          ) : null}
          <h1 className="text-[1.65rem] font-semibold tracking-tight text-cc-ink">{title}</h1>
        </div>
        {actions}
      </header>
      {children}
    </div>
  )
}

export function CcDenied({ title }: { title: string }) {
  return (
    <CommandCenterPage title={title} crumb="Command Center">
      <p className="text-sm text-cc-muted">This console is limited to platform admins.</p>
    </CommandCenterPage>
  )
}
