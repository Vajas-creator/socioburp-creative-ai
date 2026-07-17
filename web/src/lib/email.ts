import nodemailer, { type Transporter } from "nodemailer";

let transporter: Transporter | null | undefined;

/**
 * Lazily builds the SMTP transporter from env vars. Returns null (and logs a
 * warning) if SMTP isn't configured yet, so local/dev setups don't crash
 * before .env is filled in — but no email is ever faked or mocked.
 */
function getTransporter(): Transporter | null {
  if (transporter !== undefined) return transporter;

  const { SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS } = process.env;
  if (!SMTP_HOST || !SMTP_PORT || !SMTP_USER || !SMTP_PASS) {
    console.warn(
      "[email] SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS not fully configured; emails will not be sent."
    );
    transporter = null;
    return transporter;
  }

  transporter = nodemailer.createTransport({
    host: SMTP_HOST,
    port: Number(SMTP_PORT),
    secure: Number(SMTP_PORT) === 465,
    auth: { user: SMTP_USER, pass: SMTP_PASS },
  });
  return transporter;
}

export async function sendPasswordResetEmail(
  to: string,
  resetUrl: string
): Promise<void> {
  const client = getTransporter();
  const from = process.env.EMAIL_FROM ?? "SocioBurp <no-reply@socioburp.com>";

  const subject = "Reset your SocioBurp password";
  const text = `We received a request to reset your SocioBurp password. This link expires in 30 minutes:\n\n${resetUrl}\n\nIf you didn't request this, you can safely ignore this email.`;
  const html = `
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
      <h2>Reset your password</h2>
      <p>We received a request to reset your SocioBurp password. This link expires in 30 minutes.</p>
      <p><a href="${resetUrl}" style="display:inline-block;padding:10px 20px;background:#4f46e5;color:#fff;border-radius:6px;text-decoration:none;">Reset password</a></p>
      <p>If the button doesn't work, copy and paste this link into your browser:</p>
      <p><a href="${resetUrl}">${resetUrl}</a></p>
      <p>If you didn't request this, you can safely ignore this email.</p>
    </div>
  `;

  if (!client) {
    // No SMTP configured: log the real reset link server-side so local
    // dev/staging can still exercise the flow end-to-end. The API route
    // never reveals this state to the caller (see forgot-password/route.ts).
    console.warn(
      `[email] SMTP not configured. Password reset link for ${to}: ${resetUrl}`
    );
    return;
  }

  await client.sendMail({ from, to, subject, text, html });
}
