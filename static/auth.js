document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('authForm');
  if (!form) return;
  const message = document.getElementById('authMessage');
  const submit = form.querySelector('[type="submit"]');
  const mode = form.dataset.mode;

  document.querySelectorAll('[data-toggle-password]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = document.getElementById('authPassword');
      const visible = input.type === 'text';
      input.type = visible ? 'password' : 'text';
      button.textContent = visible ? 'Show' : 'Hide';
      button.setAttribute('aria-label', visible ? 'Show password' : 'Hide password');
    });
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    message.textContent = '';
    message.className = 'form-message';
    if (!form.reportValidity()) return;
    const payload = Object.fromEntries(new FormData(form).entries());
    submit.disabled = true;
    submit.textContent = mode === 'signup' ? 'Creating account…' : 'Logging in…';
    try {
      const response = await fetch(`/api/auth/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Unable to complete this request.');
      message.textContent = mode === 'signup' ? 'Account created. Opening your workspace…' : 'Login successful. Opening your workspace…';
      message.classList.add('success-message');
      window.location.assign(result.redirect || '/dashboard');
    } catch (error) {
      message.textContent = error.message || 'Request failed. Please try again.';
      message.classList.add('error-message');
    } finally {
      submit.disabled = false;
      submit.innerHTML = mode === 'signup' ? 'Create account <span>→</span>' : 'Log in <span>→</span>';
    }
  });
});
