/**
 * =================================================================
 * FINDASH 2026 - ARQUITECTURA CORE JAVASCRIPT
 * Archivo: static/js/main.js
 * =================================================================
 */

document.addEventListener('DOMContentLoaded', () => {
    initSidebar();
    initToasts();
});

/**
 * 1. GESTIÓN DEL SIDEBAR COLAPSABLE (Con persistencia)
 */
function initSidebar() {
    const btnCollapse = document.getElementById('sidebarCollapseBtn');
    const layoutWrapper = document.getElementById('layoutWrapper');
    const collapseIcon = document.getElementById('collapseIcon');

    if (btnCollapse && layoutWrapper) {
        // Recuperar estado previo guardado en el navegador
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
            
            // Forzar a los gráficos de Chart.js a recalcular su tamaño instantáneamente
            window.dispatchEvent(new Event('resize'));
        });
    }
}

/**
 * 2. ENTRADA ASÍNCRONA DE PREFERENCIAS CLOUD (Sincronización SQL)
 */
function guardarPreferenciasCloud() {
    const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
    const privacyInput = document.getElementById('toggle-privacy');
    const animationsInput = document.getElementById('toggle-animations');
    
    const privacyChecked = privacyInput ? privacyInput.checked : false;
    const animationsChecked = animationsInput ? animationsInput.checked : false;

    // Disparar envío en segundo plano a Flask sin recargar la página
    fetch("/panel/settings/preferencias", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            tema: currentTheme,
            privacidad: privacyChecked,
            animaciones: animationsChecked
        })
    })
    .then(res => res.json())
    .then(data => {
        console.log("[FinDash Sync] Preferencias grabadas en la nube SQLite.");
    })
    .catch(err => console.error("[FinDash Sync] Error crítico en pasarela Fetch:", err));
}

/**
 * 3. CONTROLADOR DE NOTIFICACIONES TOAST EFÍMERAS
 */
function initToasts() {
    setTimeout(() => {
        document.querySelectorAll('.toast').forEach(toast => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(-10px)';
            toast.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        });
    }, 3500);
}