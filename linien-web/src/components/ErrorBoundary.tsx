import React from 'react';

type ErrorBoundaryProps = {
  children: React.ReactNode;
};

type ErrorBoundaryState = {
  error?: Error;
};

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {};

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error('UI error:', error);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24 }}>
          <div className="panel" style={{ padding: 16 }}>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>UI crashed</div>
            <div style={{ fontSize: 13, marginBottom: 8 }}>
              {this.state.error.message}
            </div>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, margin: 0 }}>
              {this.state.error.stack}
            </pre>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
