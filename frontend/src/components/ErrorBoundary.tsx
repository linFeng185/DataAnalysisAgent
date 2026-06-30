import { Component, ReactNode } from 'react';
import { Result, Button } from 'antd';

interface State { hasError: boolean; error: Error | null; }
interface Props { children: ReactNode; }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <Result status="error" title="页面出错"
          subTitle={this.state.error?.message || '未知错误'}
          extra={<Button type="primary" onClick={() => this.setState({ hasError: false, error: null })}>重试</Button>}
        />
      );
    }
    return this.props.children;
  }
}
