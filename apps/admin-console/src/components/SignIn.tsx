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

/**
 * Seeded staff accounts, for the development quick-sign-in below.
 *
 * Security Officer is first because it is the one that opens every screen the
 * console has without an MFA prompt. Administrator adds tripwire configuration
 * and calibration; System Admin adds evidence redaction — the permission
 * Administrator is deliberately denied.
 */
const DEV_ACCOUNTS: Array<{ phone: string; label: string; note: string }> = [
  { phone: '9000000003', label: 'Security Officer', note: 'reviews breaches, dispatches units' },
  { phone: '9000000002', label: 'Administrator', note: 'adds calibration, tripwires, config' },
  { phone: '9000000001', label: 'System Admin', note: 'adds evidence redaction' },
]

export function SignIn() {
  const { status, error, signIn, devSignIn, verifyMfa } = useAuth()
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

        {/* Development quick sign-in.
         *
         * `import.meta.env.DEV` is a compile-time constant, so this whole block
         * — and the `devSignIn` call inside it — is removed from a production
         * bundle by dead-code elimination rather than merely hidden behind a
         * runtime check. The server route is independently dead outside
         * development, so both halves have to be wrong for this to be reachable.
         */}
        {import.meta.env.DEV && status !== 'mfa' && (
          <div className="devlogin">
            <p className="devlogin__title">
              Development sign-in — no password, no TOTP. Needs
              <code> DEV_LOGIN_ENABLED=true</code> in <code>.env</code>.
            </p>
            {DEV_ACCOUNTS.map((account) => (
              <button
                key={account.phone}
                type="button"
                className="devlogin__button"
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  try {
                    await devSignIn(account.phone)
                  } catch {
                    // `useAuth` has already put a readable message in `error`.
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                <strong>{account.label}</strong>
                <span className="mono">{account.phone}</span>
                <em>{account.note}</em>
              </button>
            ))}
          </div>
        )}
      </form>
    </main>
  )
}
