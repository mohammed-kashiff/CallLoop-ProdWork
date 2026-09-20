import type { FormEvent, ReactNode, Ref } from 'react'
import { ccBtn, ccHint, ccInput } from './classes'

export function CcSearchBar({
  value,
  onChange,
  onSubmit,
  placeholder,
  hint,
  submitLabel = 'Search',
  loading = false,
  extra,
  hideSubmit = false,
  inputRef,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit?: (e: FormEvent) => void
  placeholder: string
  hint?: string
  submitLabel?: string
  loading?: boolean
  extra?: ReactNode
  hideSubmit?: boolean
  inputRef?: Ref<HTMLInputElement>
}) {
  return (
    <form
      className="mb-6 grid gap-3"
      onSubmit={(e) => {
        if (hideSubmit) {
          e.preventDefault()
          return
        }
        onSubmit?.(e)
      }}
    >
      {hint ? <p className={ccHint}>{hint}</p> : null}
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={inputRef}
          type="search"
          className={`${ccInput} min-w-[16rem] flex-1`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          aria-label={placeholder}
        />
        {hideSubmit ? null : (
          <button type="submit" className={ccBtn} disabled={loading}>
            {loading ? 'Searching…' : submitLabel}
          </button>
        )}
        {extra}
      </div>
    </form>
  )
}
