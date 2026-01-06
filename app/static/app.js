/**
 * yt-dlp Web Client - Performance Optimized
 * Modern application with VueUse, axios, dayjs, DOMPurify
 * Optimizations: granular reactivity, minimal re-renders, cancellable requests
 */

const { createApp, ref, computed, onMounted, onUnmounted, watch, nextTick } = Vue
const { 
    useStorage, 
    useDark, 
    useToggle,
    useClipboard,
    useDebounceFn 
} = VueUse

createApp({
    setup() {
        // --- i18n Logic Start ---
        const currentLocale = useStorage('locale', 'ja')
        const translations = ref({})
        const isLocalesLoading = ref(true)

        const loadTranslations = async (lang) => {
            isLocalesLoading.value = true
            try {
                // Determine path - handle both dev and prod paths if necessary
                // Assuming standard static serving
                const response = await axios.get(`locales/${lang}.json?v=${Date.now()}`) // Cache bust for development
                translations.value = response.data
                dayjs.locale(lang) // Update dayjs locale
            } catch (e) {
                console.error(`Failed to load translations for ${lang}`, e)
                // Fallback to ja if en fails, or keep current
                if (lang !== 'ja') {
                    await loadTranslations('ja')
                }
            } finally {
                isLocalesLoading.value = false
            }
        }

        const t = (key) => {
            if (isLocalesLoading.value) return '' // or a loader placeholder
            const keys = key.split('.')
            let value = translations.value
            for (const k of keys) {
                if (value && value[k]) {
                    value = value[k]
                } else {
                    return key // Fallback to key if not found
                }
            }
            return value
        }

        const changeLanguage = async (lang) => {
            currentLocale.value = lang
            await loadTranslations(lang)
        }

        // Initialize translations
        onMounted(() => {
            loadTranslations(currentLocale.value)
        })
        // --- i18n Logic End ---

        // API Base URL management with localStorage
        const defaultApiBase = window.location.origin
        const apiBaseUrl = useStorage('apiBaseUrl', defaultApiBase)
        
        const getApiBase = () => {
            return apiBaseUrl.value || defaultApiBase
        }
        
        const saveApiBase = () => {
            // Remove trailing slashes
            apiBaseUrl.value = apiBaseUrl.value.replace(/\/+$/, '')
        }
        
        const resetApiBase = () => {
            apiBaseUrl.value = defaultApiBase
        }

        // API client with axios (singleton)
        const api = axios.create({
            timeout: 15000,
            headers: { 'Content-Type': 'application/json' }
        })
        
        // Update base URL dynamically
        api.interceptors.request.use(config => {
            config.baseURL = getApiBase()
            return config
        })

        // Disconnected toast state
        const showDisconnectedToast = ref(false)
        let disconnectToastTimeout = null
        
        const showDisconnectNotification = () => {
            showDisconnectedToast.value = true
            // Clear any existing hide timeout
            if (disconnectToastTimeout) {
                clearTimeout(disconnectToastTimeout)
                disconnectToastTimeout = null
            }
        }
        
        const hideDisconnectNotification = () => {
            // Delay hiding to avoid flicker
            if (disconnectToastTimeout) {
                clearTimeout(disconnectToastTimeout)
            }
            disconnectToastTimeout = setTimeout(() => {
                showDisconnectedToast.value = false
            }, 500)
        }
        
        const isMobileOrTablet = () => {
            return window.matchMedia('(max-width: 1024px) and (pointer: coarse)').matches
        }

        // Add response interceptor for error handling + disconnect detection
        api.interceptors.response.use(
            response => {
                hideDisconnectNotification()
                return response
            },
            error => {
                console.error('API Error:', error)
                
                // Network error or timeout
                if (!error.response || error.code === 'ECONNABORTED' || error.code === 'ERR_NETWORK') {
                    showDisconnectNotification()
                }
                
                const message = error.response?.data?.detail || error.message || t('messages.request_failed')
                return Promise.reject(new Error(message))
            }
        )

        // API Health Check - Periodic polling
        let healthCheckInterval = null
        
        const checkApiHealth = async () => {
            try {
                // Try simple GET request to root or a lightweight health endpoint
                await axios.get(`${getApiBase()}/`, { timeout: 5000 })
                hideDisconnectNotification()
            } catch (e) {
                console.warn('API health check failed:', e)
                showDisconnectNotification()
            }
        }
        
        const startHealthCheck = () => {
            // Initial check
            checkApiHealth()
            
            // Check every 30 seconds
            healthCheckInterval = setInterval(() => {
                checkApiHealth()
            }, 30000)
        }
        
        const stopHealthCheck = () => {
            if (healthCheckInterval) {
                clearInterval(healthCheckInterval)
                healthCheckInterval = null
            }
        }

        // AbortController for cancellable requests
        let abortController = null

        // Reactive state - Granular refs for better performance
        const url = ref('')
        const loading = ref(false)
        
        // Error state for Modal
        const error = ref(null)
        const showErrorModal = ref(false)
        
        // Helper to trigger error modal
        const triggerError = (msg) => {
            error.value = msg
            showErrorModal.value = true
        }

        const closeErrorModal = () => {
            showErrorModal.value = false
            // Optional: clear error after animation
            setTimeout(() => {
                error.value = null
            }, 300)
        }
        
        // Video info - split into granular refs to minimize re-renders
        const infoTitle = ref('')
        const infoUploader = ref('')
        const infoThumbnail = ref('')
        const infoViewCount = ref(0)
        const infoDuration = ref(0)
        const hasInfo = ref(false)
        
        // Available qualities (pre-computed, no re-calculation)
        const availableQualities = ref([])
        
        // Download state
        const downloading = ref(false)
        const selectedQuality = ref('')
        const currentTaskId = ref(null)
        
        // Progress - granular refs for minimal re-renders
        const progressStatus = ref(null)
        const progressValue = ref(0)
        const progressMessage = ref('')
        const progressFilename = ref('')
        const progressSpeed = ref('')
        const progressEta = ref('')
        
        // UI state
        const currentPage = ref('home')
        const showDeleteModal = ref(false)
        const showSettingsModal = ref(false)
        const showCancelModal = ref(false) // New: Cancel confirmation modal

        // VueUse: LocalStorage for history (auto-sync)
        const history = useStorage('downloadHistory', [])

        // VueUse: Dark mode
        const isDarkMode = useDark({
            selector: 'body',
            attribute: 'class',
            valueDark: 'dark',
            valueLight: 'light'
        })
        const toggleDark = useToggle(isDarkMode)

        // VueUse: Clipboard
        const { copy, copied, isSupported: clipboardSupported } = useClipboard()

        // Sanitize user-generated content
        const sanitize = (dirty) => {
            if (!dirty) return ''
            return DOMPurify.sanitize(dirty, { 
                ALLOWED_TAGS: [],
                ALLOWED_ATTR: [] 
            })
        }

        // Computed - info object for template compatibility
        const info = computed(() => {
            if (!hasInfo.value) return null
            return {
                title: infoTitle.value,
                uploader: infoUploader.value,
                thumbnail: infoThumbnail.value,
                view_count: infoViewCount.value,
                duration: infoDuration.value
            }
        })
        
        // Computed - downloadProgress object for template
        const downloadProgress = computed(() => {
            if (!progressStatus.value) return null
            return {
                status: progressStatus.value,
                progress: progressValue.value,
                message: progressMessage.value,
                filename: progressFilename.value,
                speed: progressSpeed.value,
                eta: progressEta.value
            }
        })

        // Watch progressFilename for overflow detection (marquee)
        watch(progressFilename, async (newVal) => {
            if (!newVal) return
            await nextTick()
            const progressTextEl = document.querySelector('.progress-text')
            const strongEl = progressTextEl?.querySelector('strong')
            if (progressTextEl && strongEl) {
                // Check if text overflows container
                if (strongEl.scrollWidth > progressTextEl.clientWidth) {
                    progressTextEl.classList.add('overflow')
                    strongEl.setAttribute('data-text', newVal)
                } else {
                    progressTextEl.classList.remove('overflow')
                }
            }
        })

        // History management with minimal updates (splice instead of reassign)
        const saveToHistory = (videoInfo) => {
            const historyItem = {
                url: url.value,
                title: sanitize(videoInfo.title),
                thumbnail: videoInfo.thumbnail,
                uploader: sanitize(videoInfo.uploader),
                timestamp: Date.now()
            }
            
            // Remove duplicate by URL (minimal change)
            const existingIndex = history.value.findIndex(item => item.url === url.value)
            if (existingIndex !== -1) {
                history.value.splice(existingIndex, 1)
            }
            
            // Add to beginning
            history.value.unshift(historyItem)
            
            // Trim to 20 items
            if (history.value.length > 20) {
                history.value.splice(20)
            }
        }

        const confirmClearHistory = () => {
            showDeleteModal.value = true
        }

        const clearHistory = () => {
            history.value.splice(0, history.value.length)
            showDeleteModal.value = false
        }

        const deleteHistoryItem = (index) => {
            history.value.splice(index, 1)
        }

        const loadFromHistory = (item) => {
            url.value = item.url
            currentPage.value = 'home'
            fetchInfoImmediate()
        }

        // Format timestamp with dayjs
        const formatTimestamp = (timestamp) => {
            return dayjs(timestamp).format('YYYY/MM/DD HH:mm')
        }

        const formatRelativeTime = (timestamp) => {
            return dayjs(timestamp).fromNow()
        }

        // Navigation
        const navigateToPage = (page) => {
            currentPage.value = page
        }

        // Theme management
        const toggleTheme = (event) => {
            isDarkMode.value = event.target.checked
        }

        const toggleThemeManual = () => {
            toggleDark()
        }

        // Clipboard
        const pasteFromClipboard = async () => {
            try {
                const text = await navigator.clipboard.readText()
                if (!text) {
                    triggerError(t('messages.clipboard_empty'))
                    return
                }
                url.value = text
            } catch (e) {
                triggerError(t('messages.clipboard_error'))
            }
        }

        // Video info with AbortController for cancellable requests
        const fetchInfoImmediate = async () => {
            if (!url.value) return
            
            // Cancel previous request
            if (abortController) {
                abortController.abort()
            }
            abortController = new AbortController()
            
            loading.value = true
            error.value = null
            hasInfo.value = false
            
            try {
                const response = await api.post('/info', 
                    { url: url.value }, 
                    { signal: abortController.signal }
                )
                
                const data = response.data
                
                // Update granular refs (sanitized)
                infoTitle.value = sanitize(data.title)
                infoUploader.value = sanitize(data.uploader)
                infoThumbnail.value = data.thumbnail
                infoViewCount.value = data.view_count || 0
                infoDuration.value = data.duration || 0
                hasInfo.value = true
                
                // Pre-compute available qualities (once, no re-computation)
                const qualities = new Set()
                data.formats?.forEach(format => {
                    if (format.height && format.vcodec !== 'none') {
                        qualities.add(format.height)
                    }
                })
                availableQualities.value = Array.from(qualities)
                    .filter(q => [1080, 720, 480].includes(q))
                    .sort((a, b) => b - a)
                
            } catch (e) {
                if (e.name !== 'AbortError') {
                    triggerError(e.message)
                }
            } finally {
                loading.value = false
            }
        }

        // Debounced version for auto-fetch (300ms)
        const fetchInfo = useDebounceFn(fetchInfoImmediate, 300)

        const formatDuration = (seconds) => {
            if (!seconds) return 'Live'
            const duration = dayjs.duration(seconds, 'seconds')
            const h = Math.floor(duration.asHours())
            const m = duration.minutes()
            const s = duration.seconds()
            return h > 0 
                ? `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
                : `${m}:${s.toString().padStart(2, '0')}`
        }

        // WebSocket - singleton with reconnection
        let ws = null
        let wsConnectionTimeout = null
        
        const connectWebSocket = (taskId) => {
            // Safari fix: delay WebSocket connection slightly
            if (wsConnectionTimeout) {
                clearTimeout(wsConnectionTimeout)
            }
            
            wsConnectionTimeout = setTimeout(() => {
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
                const apiBase = getApiBase()
                const host = apiBase.replace(/^https?:\/\//, '')
                const wsUrl = `${protocol}//${host}/download/progress/ws/${taskId}`
                
                // Close existing connection if any
                if (ws) {
                    ws.close()
                    ws = null
                }
                
                ws = new ReconnectingWebSocket(wsUrl, [], {
                    maxRetries: 10,
                    reconnectionDelayGrowFactor: 1.3,
                    maxReconnectionDelay: 4000,
                    minReconnectionDelay: 1000,
                    debug: false
                })
                
                ws.addEventListener('open', () => {
                    console.log('WebSocket connected')
                    hideDisconnectNotification()
                })
                
                ws.addEventListener('message', (event) => {
                    // Ignore pong messages
                    if (event.data === 'pong') return
                    
                    try {
                        const data = JSON.parse(event.data)
                        
                        // Optimized: only update changed values to minimize re-renders
                        if (data.status && data.status !== progressStatus.value) {
                            progressStatus.value = data.status
                        }
                        if (data.progress !== undefined && data.progress !== progressValue.value) {
                            progressValue.value = data.progress
                        }
                        if (data.message && data.message !== progressMessage.value) {
                            progressMessage.value = data.message
                        }
                        if (data.filename && data.filename !== progressFilename.value) {
                            progressFilename.value = data.filename
                        }
                        if (data.speed && data.speed !== progressSpeed.value) {
                            progressSpeed.value = data.speed
                        }
                        if (data.eta && data.eta !== progressEta.value) {
                            progressEta.value = data.eta
                        }
                        
                        // CHANGED: Don't auto-dismiss on completion - let user manually close
                        if (data.status === 'completed') {
                            setTimeout(() => {
                                triggerBrowserDownload(taskId)
                            }, 500)
                        } else if (data.status === 'error') {
                            // Only auto-hide on error (not on cancellation)
                            setTimeout(() => {
                                resetProgress()
                                triggerError(data.message || t('messages.download_error'))
                            }, 2000)
                        }
                        // REMOVED: auto-dismiss on 'cancelled' status
                    } catch (e) {
                        console.error('WebSocket message parse error:', e)
                    }
                })
                
                ws.addEventListener('error', (error) => {
                    console.error('WebSocket error:', error)
                    // Don't show disconnect immediately on Safari
                    setTimeout(() => {
                        if (ws && ws.readyState !== WebSocket.OPEN) {
                            showDisconnectNotification()
                        }
                    }, 1000)
                })
                
                ws.addEventListener('close', (event) => {
                    console.log('WebSocket disconnected', event.code)
                    // Only show disconnect if it wasn't a normal closure
                    if (event.code !== 1000 && event.code !== 1001 && event.code !== 1005) {
                        // Delay showing disconnect notification
                        setTimeout(() => {
                            if (progressStatus.value && progressStatus.value !== 'completed') {
                                showDisconnectNotification()
                            }
                        }, 2000)
                    }
                })

                // Send ping every 30 seconds
                const pingInterval = setInterval(() => {
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        ws.send('ping')
                    } else {
                        clearInterval(pingInterval)
                    }
                }, 30000)
            }, 300) // 300ms delay for Safari
        }
        
        // Reset progress state
        const resetProgress = () => {
            progressStatus.value = null
            progressValue.value = 0
            progressMessage.value = ''
            progressFilename.value = ''
            progressSpeed.value = ''
            progressEta.value = ''
            downloading.value = false
            currentTaskId.value = null
            
            if (wsConnectionTimeout) {
                clearTimeout(wsConnectionTimeout)
                wsConnectionTimeout = null
            }
            
            if (ws) {
                ws.close()
                ws = null
            }
        }

        // Download
        const triggerBrowserDownload = (taskId) => {
            const a = document.createElement('a')
            a.href = `${getApiBase()}/download/file/${taskId}`
            a.style.display = 'none'
            document.body.appendChild(a)
            a.click()
            
            setTimeout(() => {
                document.body.removeChild(a)
                // Don't auto-reset - let user manually close the progress bar
                
                if (info.value) {
                    saveToHistory({
                        title: infoTitle.value,
                        uploader: infoUploader.value,
                        thumbnail: infoThumbnail.value
                    })
                }
            }, 1000)
        }

        // NEW: Show cancel confirmation dialog
        const confirmCancelDownload = () => {
            showCancelModal.value = true
        }

        // UPDATED: Actually cancel the download
        const cancelDownload = async () => {
            if (!currentTaskId.value) return
            
            showCancelModal.value = false // Close confirmation dialog
            progressStatus.value = 'cancelling'
            progressMessage.value = t('messages.cancelling')
            
            try {
                await api.post(`/download/cancel/${currentTaskId.value}`)
                console.log('Download cancelled')
                // Don't auto-reset - let user see "Cancelled" message and close manually
            } catch (e) {
                console.error('Cancel error:', e)
                triggerError(t('messages.cancel_failed'))
            }
        }

        // NEW: Close progress bar (after completion or cancellation)
        const closeProgressBar = () => {
            resetProgress()
        }

        const download = async () => {
            if (!hasInfo.value) return
            downloading.value = true
            error.value = null
            
            // Initialize progress
            progressStatus.value = 'queued'
            progressValue.value = 0
            progressMessage.value = t('messages.download_started')
            
            try {
                const payload = { url: url.value }
                if (selectedQuality.value === 'audio') {
                    payload.audio_format = 'mp3'
                } else if (selectedQuality.value) {
                    payload.quality = parseInt(selectedQuality.value)
                }
                
                const response = await api.post('/download/start', payload)
                currentTaskId.value = response.data.task_id
                
                connectWebSocket(response.data.task_id)
            } catch (e) {
                triggerError(e.message)
                downloading.value = false
                resetProgress()
            }
        }

        // Ripple effect - CSS-based animation (no setTimeout needed)
        const addRipple = (event) => {
            const button = event.currentTarget
            const ripple = document.createElement('span')
            const rect = button.getBoundingClientRect()
            const size = Math.max(rect.width, rect.height)
            const x = event.clientX - rect.left - size / 2
            const y = event.clientY - rect.top - size / 2

            ripple.style.width = ripple.style.height = size + 'px'
            ripple.style.left = x + 'px'
            ripple.style.top = y + 'px'
            ripple.classList.add('ripple')

            button.appendChild(ripple)
            
            // Auto-remove after CSS animation completes
            ripple.addEventListener('animationend', () => {
                ripple.remove()
            })
        }

        // Lifecycle: Start health check on mount
        onMounted(() => {
            startHealthCheck()
        })

        // Cleanup on unmount
        onUnmounted(() => {
            // Stop health check
            stopHealthCheck()
            
            // Cancel pending requests
            if (abortController) {
                abortController.abort()
            }
            
            // Clear WebSocket timeout
            if (wsConnectionTimeout) {
                clearTimeout(wsConnectionTimeout)
            }
            
            // Close WebSocket
            if (ws) {
                ws.close()
                ws = null
            }
            
            // Clear disconnect toast timeout
            if (disconnectToastTimeout) {
                clearTimeout(disconnectToastTimeout)
            }
        })

        // Return public API
        return {
            url, info, loading, downloading, error, selectedQuality,
            currentPage, showDeleteModal, showSettingsModal, showCancelModal, history, isDarkMode, downloadProgress,
            availableQualities, currentTaskId, hasInfo,
            fetchInfo: fetchInfoImmediate, formatDuration, download, cancelDownload, confirmCancelDownload, closeProgressBar, navigateToPage,
            pasteFromClipboard, confirmClearHistory, clearHistory, deleteHistoryItem, loadFromHistory, 
            toggleTheme, toggleThemeManual, addRipple, formatTimestamp, formatRelativeTime,
            // API Base URL management
            apiBaseUrl, defaultApiBase, saveApiBase, resetApiBase,
            // Disconnected toast
            showDisconnectedToast, isMobileOrTablet,
            // Error Modal
            showErrorModal, closeErrorModal,
            // i18n
            t, currentLocale, changeLanguage, isLocalesLoading
        }
    }
}).mount('#app')
