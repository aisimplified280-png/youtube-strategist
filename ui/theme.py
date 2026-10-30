"""Custom Streamlit UI theme and CSS injection for YouTube Strategist."""

import streamlit as st


def inject_theme():
    """Inject polished, modern dark-first styling with high visual hierarchy."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

        /* Root Typography & Foundation */
        *, *::before, *::after {
            box-sizing: border-box;
        }
        html, body {
            overflow-x: hidden;
            max-width: 100vw;
        }
        html, body, [class*="css"] {
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            -webkit-font-smoothing: antialiased;
        }
        pre, code {
            white-space: pre-wrap !important;
            word-break: break-word !important;
            overflow-wrap: anywhere !important;
        }
        .diff-panel, .copilot-video-card, .block-container {
            word-break: break-word;
            overflow-wrap: anywhere;
        }

        code, pre, .mono {
            font-family: 'JetBrains Mono', monospace !important;
        }

        /* Container constraints & padding - Claude-inspired central column (850-900px) */
        .block-container {
            max-width: 880px !important;
            padding-top: 1.25rem !important;
            padding-bottom: 5rem !important;
            padding-left: 1.25rem !important;
            padding-right: 1.25rem !important;
        }

        /* Mobile touch responsiveness: min 44px targets */
        button, [role="button"], input, select, textarea {
            min-height: 44px;
        }

        /* Video Context Card */
        .copilot-video-card {
            background: #111827;
            border: 1px solid rgba(255, 255, 255, 0.09);
            border-radius: 12px;
            padding: 16px 20px;
            margin-bottom: 16px;
        }
        .copilot-video-title {
            font-size: 1.15rem;
            font-weight: 700;
            color: #FFFFFF;
            line-height: 1.35;
            margin: 0 0 6px 0;
        }
        .copilot-metrics-strip {
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 8px 12px;
            background: #1A2234;
            border-radius: 8px;
            font-size: 0.85rem;
            color: #CBD5E1;
            margin-top: 10px;
            flex-wrap: wrap;
        }

        /* Hero Action Card */
        .hero-action-card {
            background: linear-gradient(135deg, rgba(26, 32, 46, 0.95) 0%, rgba(18, 22, 33, 0.98) 100%);
            border: 1px solid rgba(255, 30, 68, 0.35);
            border-radius: 16px;
            padding: 24px 28px;
            margin-bottom: 24px;
            box-shadow: 0 10px 30px -5px rgba(255, 30, 68, 0.12), 0 4px 12px rgba(0, 0, 0, 0.4);
            position: relative;
            overflow: hidden;
        }
        .hero-action-card::before {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, #FF1E44, #FF6B81, #FF1E44);
        }
        .hero-tag {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 12px;
            background: rgba(255, 30, 68, 0.15);
            border: 1px solid rgba(255, 30, 68, 0.4);
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 700;
            color: #FF4D6D;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 12px;
        }
        .hero-title {
            font-size: 1.45rem;
            font-weight: 800;
            letter-spacing: -0.025em;
            color: #FFFFFF;
            margin-bottom: 8px;
            line-height: 1.25;
        }
        .hero-subtitle {
            font-size: 0.95rem;
            color: #94A3B8;
            line-height: 1.55;
            margin-bottom: 16px;
        }

        /* Metric Tiles */
        .metric-card {
            background: #161C28;
            border: 1px solid rgba(255, 255, 255, 0.07);
            border-radius: 12px;
            padding: 16px 18px;
            transition: border-color 0.15s ease, transform 0.15s ease;
        }
        .metric-card:hover {
            border-color: rgba(255, 255, 255, 0.15);
            transform: translateY(-1px);
        }
        .metric-label {
            font-size: 0.8rem;
            font-weight: 600;
            color: #94A3B8;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 4px;
        }
        .metric-value {
            font-size: 1.7rem;
            font-weight: 800;
            color: #F8FAFC;
            letter-spacing: -0.02em;
            line-height: 1.2;
        }
        .metric-delta {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 0.8rem;
            font-weight: 600;
            margin-top: 6px;
        }
        .delta-positive { color: #10B981; }
        .delta-negative { color: #EF4444; }
        .delta-neutral { color: #94A3B8; }

        /* Evidence Chips */
        .evidence-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 3px 10px;
            border-radius: 8px;
            font-size: 0.78rem;
            font-weight: 600;
            margin-right: 6px;
            margin-bottom: 6px;
        }
        .chip-neutral {
            background: rgba(148, 163, 184, 0.12);
            color: #CBD5E1;
            border: 1px solid rgba(148, 163, 184, 0.2);
        }
        .chip-positive {
            background: rgba(16, 185, 129, 0.12);
            color: #34D399;
            border: 1px solid rgba(16, 185, 129, 0.25);
        }
        .chip-warning {
            background: rgba(245, 158, 11, 0.12);
            color: #FBBF24;
            border: 1px solid rgba(245, 158, 11, 0.25);
        }
        .chip-alert {
            background: rgba(239, 68, 68, 0.12);
            color: #F87171;
            border: 1px solid rgba(239, 68, 68, 0.25);
        }

        /* Status Pills */
        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.78rem;
            font-weight: 600;
        }
        .status-connected {
            background: rgba(16, 185, 129, 0.12);
            color: #10B981;
            border: 1px solid rgba(16, 185, 129, 0.25);
        }
        .status-reconnect {
            background: rgba(245, 158, 11, 0.15);
            color: #FBBF24;
            border: 1px solid rgba(245, 158, 11, 0.35);
        }
        .status-disconnected {
            background: rgba(148, 163, 184, 0.1);
            color: #94A3B8;
            border: 1px solid rgba(148, 163, 184, 0.2);
        }

        /* Stepper */
        .stepper-container {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: #141824;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 12px;
            padding: 12px 18px;
            margin-bottom: 24px;
        }
        .step-item {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 0.85rem;
            font-weight: 600;
            color: #64748B;
        }
        .step-item.active {
            color: #FFFFFF;
        }
        .step-item.completed {
            color: #10B981;
        }
        .step-num {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.75rem;
            font-weight: 700;
            background: rgba(255, 255, 255, 0.1);
            color: #94A3B8;
        }
        .step-item.active .step-num {
            background: #FF1E44;
            color: white;
            box-shadow: 0 0 10px rgba(255, 30, 68, 0.4);
        }
        .step-item.completed .step-num {
            background: #10B981;
            color: white;
        }
        .step-arrow {
            color: #334155;
            font-size: 1.1rem;
        }

        /* Demo Mode Banner */
        .demo-banner {
            background: linear-gradient(90deg, #3B82F6 0%, #1D4ED8 100%);
            color: white;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.25);
        }

        /* Diff Viewer */
        .diff-panel {
            background: #111520;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 14px 16px;
            margin-bottom: 12px;
            font-size: 0.9rem;
            line-height: 1.5;
        }
        .diff-added {
            background: rgba(16, 185, 129, 0.22);
            color: #6EE7B7;
            padding: 1px 4px;
            border-radius: 4px;
            font-weight: 600;
        }
        .diff-removed {
            background: rgba(239, 68, 68, 0.22);
            color: #FCA5A5;
            text-decoration: line-through;
            padding: 1px 4px;
            border-radius: 4px;
        }

        /* Character Counter Badge */
        .char-counter {
            font-size: 0.75rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 6px;
        }
        .char-ok { background: rgba(16, 185, 129, 0.15); color: #34D399; }
        .char-warn { background: rgba(245, 158, 11, 0.18); color: #FBBF24; }
        .char-over { background: rgba(239, 68, 68, 0.22); color: #F87171; }

        /* Provider Model Badge */
        .provider-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 3px 10px;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }
        .provider-groq {
            background: rgba(249, 115, 22, 0.15);
            color: #FB923C;
            border: 1px solid rgba(249, 115, 22, 0.3);
        }
        .provider-gemini {
            background: rgba(59, 130, 246, 0.15);
            color: #60A5FA;
            border: 1px solid rgba(59, 130, 246, 0.3);
        }

        /* Buttons Enhancements */
        [data-testid="stButton"] button {
            border-radius: 10px;
            font-weight: 600;
            min-height: 2.6rem;
            transition: all 0.15s ease;
        }
        [data-testid="stButton"] button[kind="primary"] {
            background: linear-gradient(135deg, #FF1E44 0%, #D80027 100%) !important;
            border: none !important;
            box-shadow: 0 4px 14px rgba(255, 30, 68, 0.3) !important;
            color: #FFFFFF !important;
        }
        [data-testid="stButton"] button[kind="primary"]:hover {
            filter: brightness(1.1);
            transform: translateY(-1px);
            box-shadow: 0 6px 18px rgba(255, 30, 68, 0.45) !important;
        }

        /* Mobile Adjustments (360px - 768px) */
        @media (max-width: 768px) {
            .block-container {
                max-width: 100% !important;
                overflow-x: hidden !important;
                padding-top: 0.75rem !important;
                padding-left: 0.75rem !important;
                padding-right: 0.75rem !important;
                padding-bottom: 7rem !important;
            }
            [data-testid="stHorizontalBlock"] {
                flex-wrap: wrap !important;
            }
            .copilot-video-card {
                padding: 12px 14px;
            }
            .copilot-video-title {
                font-size: 1rem;
            }
            .copilot-metrics-strip {
                gap: 8px;
                padding: 8px 10px;
                font-size: 0.78rem;
            }
            [data-testid="stChatInput"] {
                padding-left: 0.5rem !important;
                padding-right: 0.5rem !important;
            }
            [data-testid="stChatInput"] textarea {
                font-size: 0.92rem !important;
            }
            [data-testid="stFileUploader"] {
                width: 100%;
            }
            [data-testid="stButton"] button {
                min-height: 2.85rem;
                width: 100%;
            }
            .hero-action-card {
                padding: 16px 18px;
            }
            .hero-title {
                font-size: 1.2rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
