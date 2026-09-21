import type { ReactNode } from 'react'
import { ccTh } from './classes'

export function CcTable({
  columns,
  children,
  empty,
  footer,
}: {
  columns: ReactNode[]
  children: ReactNode
  empty?: ReactNode
  footer?: ReactNode
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-cc-line bg-cc-card shadow-sm">
      <table className="w-full min-w-[40rem] border-collapse text-left">
        <thead className="sticky top-0 bg-cc-paper">
          <tr className="border-b border-cc-line">
            {columns.map((c, i) => (
              <th key={i} className={ccTh}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
      {empty}
      {footer ? <div className="border-t border-cc-line px-3 py-2.5">{footer}</div> : null}
    </div>
  )
}

export function CcEmpty({
  title,
  body,
  action,
}: {
  title: string
  body?: string
  action?: ReactNode
}) {
  return (
    <div className="rounded-lg border border-dashed border-cc-line px-6 py-12 text-center">
      <p className="text-[17px] font-semibold text-cc-navy">{title}</p>
      {body ? <p className="mt-1 text-[15px] text-cc-muted">{body}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  )
}
