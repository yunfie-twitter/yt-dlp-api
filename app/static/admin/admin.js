const { createApp, ref, reactive, onMounted, computed } = Vue;

createApp({
    setup() {
        const token = ref(localStorage.getItem('admin_token') || '');
        const currentView = ref('dashboard');
        const loginForm = reactive({ password: '' });
        const loginError = ref('');
        const ssoEnabled = ref(false); // Should be fetched from public info
        
        const config = ref(null);
        const stats = ref({});
        const users = ref([]);
        const toast = reactive({ show: false, message: '' });

        const isAuthenticated = computed(() => !!token.value);

        // Fetch Initial Public Info (e.g. is SSO enabled?)
        const checkSSO = async () => {
             // Implementation depends on if we have a public config endpoint
             // For now assume hardcoded or handled via error message
        };

        const login = async () => {
            try {
                const res = await fetch('/admin/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: loginForm.password })
                });
                
                if (!res.ok) throw new Error(await res.text());
                
                const data = await res.json();
                token.value = data.access_token;
                localStorage.setItem('admin_token', token.value);
                loginError.value = '';
                fetchData();
            } catch (e) {
                loginError.value = 'Login Failed: ' + e.message;
            }
        };
        
        const loginGoogle = () => {
            window.location.href = '/admin/login/google';
        };

        const logout = () => {
            token.value = '';
            localStorage.removeItem('admin_token');
        };

        const showToast = (msg) => {
            toast.message = msg;
            toast.show = true;
            setTimeout(() => toast.show = false, 3000);
        };

        const authenticatedFetch = async (url, options = {}) => {
            const headers = {
                'Authorization': `Bearer ${token.value}`,
                'Content-Type': 'application/json',
                ...options.headers
            };
            
            const res = await fetch(url, { ...options, headers });
            if (res.status === 401) {
                logout();
                throw new Error('Unauthorized');
            }
            return res;
        };

        const fetchConfig = async () => {
            try {
                const res = await authenticatedFetch('/admin/config');
                config.value = await res.json();
            } catch (e) {
                console.error(e);
            }
        };

        const saveConfig = async () => {
            try {
                const res = await authenticatedFetch('/admin/config', {
                    method: 'PUT',
                    body: JSON.stringify(config.value)
                });
                if (res.ok) showToast('Settings Saved!');
            } catch (e) {
                showToast('Error Saving Settings');
            }
        };

        const fetchStats = async () => {
            // Mock stats for now
            stats.value = { active_downloads: 0 };
        };

        const fetchData = () => {
            if (isAuthenticated.value) {
                fetchConfig();
                fetchStats();
            }
        };

        onMounted(() => {
            checkSSO();
            if (isAuthenticated.value) {
                fetchData();
            }
        });

        return {
            token,
            currentView,
            loginForm,
            loginError,
            ssoEnabled,
            isAuthenticated,
            config,
            stats,
            users,
            toast,
            login,
            loginGoogle,
            logout,
            fetchConfig,
            saveConfig
        };
    }
}).mount('#app');
