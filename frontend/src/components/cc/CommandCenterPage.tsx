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
    <div className="mx-auto w-full max-w-[1400px] px-1 pb-16">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          {crumb ? (
            <p className="mb-1.5 text-[14px] font-semibold text-cc-accent">
              {crumb}
            </p>
          ) : null}
          <h1 className="text-[2rem] font-bold leading-tight tracking-tight text-cc-navy">{title}</h1>
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
      <p className="text-[16px] text-cc-muted">This console is limited to platform admins.</p>
    </CommandCenterPage>
  )
}
