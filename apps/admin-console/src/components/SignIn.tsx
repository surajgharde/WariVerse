/**
 * Staff sign-in.
 *
 * Two steps for the roles that need it: password, then a TOTP code for
 * Administrator and System Admin. The MFA challenge lives in React state and
 * nowhere else — a half-finished sign-in that survives a reload is one somebody
 * can walk up to on a shared control-room workstation.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'

import { useI18n } from '@/i18n'
import { useAuth } from '@/state/auth'

export function SignIn() {
  const { status, error, signIn, verifyMfa } = useAuth()
  const { t } = useI18n()

  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    try {
      if (status === 'mfa') {
        await verifyMfa(code)
      } else {
        await signIn(phone, password)
      }
    } catch {
      // `useAuth` has already put a readable message in `error`.
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="signin">
      <form className="signin__card" onSubmit={submit}>
        <h1 className="signin__title">{t('app.title')}</h1>
        <p className="signin__subtitle">{t('app.subtitle')}</p>

        {status === 'mfa' ? (
          <>
            <p className="signin__prompt">{t('auth.mfaPrompt')}</p>
            <label className="field">
              <span>{t('auth.mfaCode')}</span>
              <input
                className="mono"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                inputMode="numeric"
                autoComplete="one-time-code"
                autoFocus
                required
              />
            </label>
          </>
        ) : (
          <>
            <label className="field">
              <span>{t('auth.phone')}</span>
              <input
                className="mono"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                inputMode="tel"
                autoComplete="username"
                autoFocus
                required
              />
            </label>
            <label className="field">
              <span>{t('auth.password')}</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
          </>
        )}

        {error && (
          <p className="signin__error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="btn btn--primary btn--wide" disabled={busy}>
          {busy ? t('auth.working') : t('auth.signIn')}
        </button>
      </form>
    </main>
  )
}
