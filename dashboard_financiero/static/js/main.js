/**
 * =================================================================
 * FINDASH 2026 - ARQUITECTURA CORE JAVASCRIPT (VERSION ULTRA-UX)
 * Archivo: static/js/main.js
 * =================================================================
 */

// Aplicar tema guardado ANTES de que el DOM pinte (evita flash)
(function() {
    const savedTheme = localStorage.getItem('findash_theme');
    if (savedTheme && savedTheme !== 'system') {
        document.documentElement.setAttribute('data-theme', savedTheme);
    } else if (savedTheme === 'system') {
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.setAttribute('data-theme', prefersDark ? 'dark' : 'light');
    }
})();

document.addEventListener('DOMContentLoaded', () => {
    try {
        initSidebar();
        initToasts();
        animateAllCards(); // Entrada fluida en cualquier pestaña
        setTimeout(animateCounters, 150); // Animación de números blindada
    } catch (e) {
        console.error("[FinDash UX Error] Error en inicialización:", e);
    }
});

/**
 * 1. GESTIÓN DEL SIDEBAR COLAPSABLE (Con persistencia)
 */
function initSidebar() {
    const btnCollapse = document.getElementById('sidebarCollapseBtn');
    const layoutWrapper = document.getElementById('layoutWrapper');
    const collapseIcon = document.getElementById('collapseIcon');

    if (!btnCollapse || !layoutWrapper) return;

    if (localStorage.getItem('findash_sidebar_collapsed') === 'true') {
        layoutWrapper.classList.add('collapsed');
        if (collapseIcon) collapseIcon.className = 'ph ph-caret-right';
    }

    btnCollapse.addEventListener('click', () => {
        const isCollapsed = layoutWrapper.classList.toggle('collapsed');
        if (collapseIcon) {
            collapseIcon.className = isCollapsed ? 'ph ph-caret-right' : 'ph ph-caret-left';
        }
        localStorage.setItem('findash_sidebar_collapsed', isCollapsed);
        window.dispatchEvent(new Event('resize'));
    });
}

/**
 * 2. CONTROLADOR DE TEMA VISUAL
 */
function uiSetTheme(theme) {
    let effectiveTheme = theme;
    if (theme === 'system') {
        effectiveTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', effectiveTheme);
    localStorage.setItem('findash_theme', theme);

    document.querySelectorAll('.theme-box').forEach(b => b.classList.remove('active'));
    const targetBox = document.getElementById('theme-' + theme);
    if (targetBox) targetBox.classList.add('active');

    guardarPreferenciasCloud();
}

/**
 * 3. SINCRONIZACIÓN DE PREFERENCIAS CON FLASK (Debounced)
 */
let prefSyncTimeout = null;
function guardarPreferenciasCloud() {
    clearTimeout(prefSyncTimeout);
    prefSyncTimeout = setTimeout(() => {
        const currentTheme = localStorage.getItem('findash_theme') || 'dark';
        const privacyInput = document.getElementById('toggle-privacy');
        const animationsInput = document.getElementById('toggle-animations');

        fetch("/panel/settings/preferencias", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                tema: currentTheme,
                privacidad: privacyInput ? privacyInput.checked : false,
                animaciones: animationsInput ? animationsInput.checked : false
            })
        })
        .then(res => res.json())
        .catch(err => console.error("[FinDash] Error al sincronizar:", err));
    }, 500);
}

/**
 * 4. CONTROLADOR DE NOTIFICACIONES TOAST
 */
function initToasts() {
    document.querySelectorAll('.toast').forEach(toast => {
        const isDanger = toast.classList.contains('danger');
        const delay = isDanger ? 6000 : 3500;

        setTimeout(() => {
            toast.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(-10px)';
            setTimeout(() => toast.remove(), 400);
        }, delay);
    });
}

/**
 * 5. CONTROLADOR DE VISIBILIDAD DE CONTRASEÑAS
 */
function togglePassword(inputId, btnElement) {
    const input = document.getElementById(inputId);
    const icon = btnElement.querySelector('i');
    if (!input || !icon) return;

    if (input.type === 'password') {
        input.type = 'text';
        icon.classList.replace('ph-eye', 'ph-eye-slash');
        icon.style.color = 'var(--primary)';
    } else {
        input.type = 'password';
        icon.classList.replace('ph-eye-slash', 'ph-eye');
        icon.style.color = 'var(--text-muted)';
    }
}

/**
 * 6. ANIMACIÓN DE CONTADORES NUMÉRICOS (A PRUEBA DE FALLOS Y NEGATIVOS)
 */
function animateCounters() {
    document.querySelectorAll('.animate-number').forEach(counter => {
        try {
            const finalText = counter.getAttribute('data-final-text');
            if (!finalText) return;

            // Detección inteligente de números negativos y ceros para cuentas nuevas
            const isNegative = finalText.includes('-');
            const cleanText = finalText.replace(/[^0-9,]/g, "").replace(',', '.');
            let target = parseFloat(cleanText) || 0;
            if (isNegative) target = -target; // Restauramos el símbolo negativo si lo tenía

            // Si el valor es exactamente 0 (cuenta nueva), lo pintamos directamente y terminamos
            if (target === 0) {
                counter.innerText = finalText;
                return;
            }

            let current = 0;
            const duration = 800; // Duración fluida
            const frameRate = 1000 / 60;
            const totalFrames = duration / frameRate;
            const increment = target / totalFrames;
            let frame = 0;

            const updateCount = () => {
                frame++;
                current += increment;

                if (frame < totalFrames) {
                    // Animamos con formato europeo
                    counter.innerText = current.toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
                    requestAnimationFrame(updateCount);
                } else {
                    // Al final inyectamos el texto inmaculado de Flask
                    counter.innerText = finalText;
                }
            };
            updateCount();
        } catch (err) {
            // Sistema de recuperación: si algo falla, muestra el valor real de inmediato
            counter.innerText = counter.getAttribute('data-final-text') || "0,00 €";
        }
    });
}

/**
 * 7. ENTRADA ESCALONADA UNIVERSAL (DASHBOARD, ANALYTICS, SETTINGS)
 */
function animateAllCards() {
    // AQUÍ ESTÁ EL TRUCO: Hemos añadido TODAS las clases de todas las pantallas
    const cards = document.querySelectorAll('.bento-card, .kpi-card, .analytics-card-kpi, .category-rank-item');

    let visibleIndex = 0;

    cards.forEach((card) => {
        // Comprobamos si la tarjeta está visible en la pantalla (vital para Settings)
        if (getComputedStyle(card).display !== 'none') {
            if (!card.classList.contains('fade-in-up')) {
                card.classList.add('fade-in-up');
                const delayClass = `delay-${Math.min(visibleIndex + 1, 5)}`;
                card.classList.add(delayClass);
                visibleIndex++;
            }
        } else {
            // Si está oculta (pestañas de settings no activas), la "reseteamos"
            // para que se anime mágicamente cuando el usuario haga clic en su pestaña
            card.classList.remove('fade-in-up', 'delay-1', 'delay-2', 'delay-3', 'delay-4', 'delay-5');
        }
    });
}