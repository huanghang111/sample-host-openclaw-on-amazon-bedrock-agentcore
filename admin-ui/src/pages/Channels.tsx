import { useState, useEffect } from 'react';
import {
  Card,
  Row,
  Col,
  Button,
  Input,
  Form,
  Tag,
  Spin,
  Typography,
  message,
  Popconfirm,
  Tooltip,
  Table,
  Modal,
  Select,
  Switch,
  Space,
  Divider,
} from 'antd';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  SendOutlined,
  DeleteOutlined,
  SaveOutlined,
  PlusOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import { get, put, post, del } from '../services/api';

const { Title, Text } = Typography;

interface ChannelInfo {
  name: string;
  configured: boolean;
  webhookUrl: string;
}

interface ChannelsResponse {
  channels: ChannelInfo[];
}

interface BotEntry {
  id: string;
  channel: string;
  enabled: boolean;
  hasCredentials: boolean;
}

interface BotsResponse {
  bots: BotEntry[];
}

export default function Channels() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  // Multi-bot state
  const [bots, setBots] = useState<BotEntry[]>([]);
  const [botsLoading, setBotsLoading] = useState(true);
  const [addBotOpen, setAddBotOpen] = useState(false);
  const [addBotForm] = Form.useForm();

  const [telegramForm] = Form.useForm();
  const [slackForm] = Form.useForm();
  const [feishuForm] = Form.useForm();
  const [dingtalkForm] = Form.useForm();

  const addBotChannel = Form.useWatch('channel', addBotForm);

  const fetchChannels = () => {
    setLoading(true);
    get<ChannelsResponse>('/api/channels')
      .then((data) => setChannels(data.channels))
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  const fetchBots = () => {
    setBotsLoading(true);
    get<BotsResponse>('/api/ws-bridge/bots')
      .then((data) => setBots(data.bots))
      .catch(console.error)
      .finally(() => setBotsLoading(false));
  };

  useEffect(() => {
    fetchChannels();
    fetchBots();
  }, []);

  const copyWebhookUrl = (url: string) => {
    navigator.clipboard.writeText(url);
    message.success('Webhook URL copied');
  };

  const handleSaveTelegram = async (values: { botToken: string }) => {
    setSaving('telegram');
    try {
      await put('/api/channels/telegram', { botToken: values.botToken });
      message.success('Telegram credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save Telegram credentials');
    }
    setSaving(null);
  };

  const handleRegisterWebhook = async () => {
    setSaving('telegram-webhook');
    try {
      const result = await post<{ telegramResponse: { ok: boolean } }>(
        '/api/channels/telegram/webhook',
        {}
      );
      if (result.telegramResponse?.ok) {
        message.success('Telegram webhook registered');
      } else {
        message.warning('Telegram API returned an unexpected response');
      }
    } catch {
      message.error('Failed to register Telegram webhook');
    }
    setSaving(null);
  };

  const handleSaveSlack = async (values: {
    botToken: string;
    signingSecret: string;
  }) => {
    setSaving('slack');
    try {
      await put('/api/channels/slack', values);
      message.success('Slack credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save Slack credentials');
    }
    setSaving(null);
  };

  const handleSaveFeishu = async (values: {
    appId: string;
    appSecret: string;
    verificationToken: string;
    encryptKey: string;
  }) => {
    setSaving('feishu');
    try {
      await put('/api/channels/feishu', values);
      message.success('Feishu credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save Feishu credentials');
    }
    setSaving(null);
  };

  const handleSaveDingtalk = async (values: {
    clientId: string;
    clientSecret: string;
  }) => {
    setSaving('dingtalk');
    try {
      await put('/api/channels/dingtalk', values);
      message.success('DingTalk credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save DingTalk credentials');
    }
    setSaving(null);
  };

  const handleClear = async (channel: string) => {
    setSaving(channel);
    try {
      await del(`/api/channels/${channel}`);
      message.success(`${channel} credentials cleared`);
      fetchChannels();
    } catch {
      message.error(`Failed to clear ${channel} credentials`);
    }
    setSaving(null);
  };

  // --- Multi-bot handlers ---

  const handleAddBot = async (values: {
    id: string;
    channel: string;
    clientId?: string;
    clientSecret?: string;
    appId?: string;
    appSecret?: string;
  }) => {
    const credentials =
      values.channel === 'dingtalk'
        ? { clientId: values.clientId, clientSecret: values.clientSecret }
        : { appId: values.appId, appSecret: values.appSecret };

    try {
      await post('/api/ws-bridge/bots', {
        id: values.id,
        channel: values.channel,
        enabled: true,
        credentials,
      });
      message.success(`Bot '${values.id}' added`);
      setAddBotOpen(false);
      addBotForm.resetFields();
      fetchBots();
    } catch {
      message.error('Failed to add bot');
    }
  };

  const handleToggleBot = async (botId: string, enabled: boolean) => {
    try {
      await put(`/api/ws-bridge/bots/${botId}`, { enabled });
      message.success(`Bot '${botId}' ${enabled ? 'enabled' : 'disabled'}`);
      fetchBots();
    } catch {
      message.error('Failed to update bot');
    }
  };

  const handleDeleteBot = async (botId: string) => {
    try {
      await del(`/api/ws-bridge/bots/${botId}`);
      message.success(`Bot '${botId}' deleted`);
      fetchBots();
    } catch {
      message.error('Failed to delete bot');
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 100 }}>
        <Spin size="large" />
      </div>
    );
  }

  const channelMap: Record<string, ChannelInfo> = {};
  for (const ch of channels) {
    channelMap[ch.name] = ch;
  }

  const renderStatusBadge = (configured: boolean) =>
    configured ? (
      <Tag icon={<CheckCircleOutlined />} color="success">
        Configured
      </Tag>
    ) : (
      <Tag icon={<CloseCircleOutlined />} color="default">
        Not Configured
      </Tag>
    );

  const botColumns = [
    {
      title: 'Bot ID',
      dataIndex: 'id',
      key: 'id',
    },
    {
      title: 'Channel',
      dataIndex: 'channel',
      key: 'channel',
      render: (ch: string) => (
        <Tag color={ch === 'dingtalk' ? 'blue' : 'cyan'}>{ch}</Tag>
      ),
    },
    {
      title: 'Status',
      key: 'enabled',
      render: (_: unknown, record: BotEntry) => (
        <Switch
          checked={record.enabled}
          onChange={(checked) => handleToggleBot(record.id, checked)}
          checkedChildren="On"
          unCheckedChildren="Off"
          size="small"
        />
      ),
    },
    {
      title: 'Credentials',
      key: 'hasCredentials',
      render: (_: unknown, record: BotEntry) =>
        record.hasCredentials ? (
          <Tag color="success">Set</Tag>
        ) : (
          <Tag color="warning">Missing</Tag>
        ),
    },
    {
      title: 'Action',
      key: 'action',
      render: (_: unknown, record: BotEntry) => (
        <Popconfirm
          title={`Delete bot '${record.id}'?`}
          onConfirm={() => handleDeleteBot(record.id)}
        >
          <Button danger size="small" icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Title level={4}>Channels</Title>
      <Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
        Webhook-based channels (Router Lambda)
      </Text>

      <Row gutter={[16, 16]}>
        {/* Telegram */}
        <Col xs={24} lg={6}>
          <Card
            title="Telegram"
            extra={renderStatusBadge(channelMap.telegram?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'telegram' ? null : 'telegram')
            }
          >
            {channelMap.telegram?.webhookUrl && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Webhook URL:</Text>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <Text code style={{ fontSize: 11, flex: 1 }}>
                    {channelMap.telegram.webhookUrl}
                  </Text>
                  <Tooltip title="Copy">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        copyWebhookUrl(channelMap.telegram.webhookUrl);
                      }}
                    />
                  </Tooltip>
                </div>
              </div>
            )}

            {expanded === 'telegram' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={telegramForm}
                  layout="vertical"
                  onFinish={handleSaveTelegram}
                >
                  <Form.Item
                    name="botToken"
                    label="Bot Token"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="123456:ABC-DEF..." />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'telegram'}
                    >
                      Save
                    </Button>
                    <Button
                      icon={<SendOutlined />}
                      loading={saving === 'telegram-webhook'}
                      onClick={handleRegisterWebhook}
                    >
                      Register Webhook
                    </Button>
                    <Popconfirm
                      title="Clear Telegram credentials?"
                      onConfirm={() => handleClear('telegram')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
              </div>
            )}
          </Card>
        </Col>

        {/* Slack */}
        <Col xs={24} lg={6}>
          <Card
            title="Slack"
            extra={renderStatusBadge(channelMap.slack?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'slack' ? null : 'slack')
            }
          >
            {channelMap.slack?.webhookUrl && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Webhook URL:</Text>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <Text code style={{ fontSize: 11, flex: 1 }}>
                    {channelMap.slack.webhookUrl}
                  </Text>
                  <Tooltip title="Copy">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        copyWebhookUrl(channelMap.slack.webhookUrl);
                      }}
                    />
                  </Tooltip>
                </div>
              </div>
            )}

            {expanded === 'slack' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={slackForm}
                  layout="vertical"
                  onFinish={handleSaveSlack}
                >
                  <Form.Item
                    name="botToken"
                    label="Bot Token"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="xoxb-..." />
                  </Form.Item>
                  <Form.Item
                    name="signingSecret"
                    label="Signing Secret"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Signing secret" />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'slack'}
                    >
                      Save
                    </Button>
                    <Popconfirm
                      title="Clear Slack credentials?"
                      onConfirm={() => handleClear('slack')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
                <Text
                  type="secondary"
                  style={{ display: 'block', marginTop: 12, fontSize: 12 }}
                >
                  Copy the webhook URL above and paste it into your Slack app's
                  Event Subscriptions Request URL field.
                </Text>
              </div>
            )}
          </Card>
        </Col>

        {/* Feishu (Webhook) */}
        <Col xs={24} lg={6}>
          <Card
            title="Feishu"
            extra={renderStatusBadge(channelMap.feishu?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'feishu' ? null : 'feishu')
            }
          >
            {channelMap.feishu?.webhookUrl && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Webhook URL:</Text>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <Text code style={{ fontSize: 11, flex: 1 }}>
                    {channelMap.feishu.webhookUrl}
                  </Text>
                  <Tooltip title="Copy">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        copyWebhookUrl(channelMap.feishu.webhookUrl);
                      }}
                    />
                  </Tooltip>
                </div>
              </div>
            )}

            {expanded === 'feishu' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={feishuForm}
                  layout="vertical"
                  onFinish={handleSaveFeishu}
                >
                  <Form.Item
                    name="appId"
                    label="App ID"
                    rules={[{ required: true }]}
                  >
                    <Input placeholder="cli_..." />
                  </Form.Item>
                  <Form.Item
                    name="appSecret"
                    label="App Secret"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="App secret" />
                  </Form.Item>
                  <Form.Item
                    name="verificationToken"
                    label="Verification Token"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Verification token" />
                  </Form.Item>
                  <Form.Item
                    name="encryptKey"
                    label="Encrypt Key"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Encrypt key" />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'feishu'}
                    >
                      Save
                    </Button>
                    <Popconfirm
                      title="Clear Feishu credentials?"
                      onConfirm={() => handleClear('feishu')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
                <Text
                  type="secondary"
                  style={{ display: 'block', marginTop: 12, fontSize: 12 }}
                >
                  Copy the webhook URL above and paste it into your Feishu app's
                  Event Subscriptions Request URL field.
                </Text>
              </div>
            )}
          </Card>
        </Col>

        {/* DingTalk */}
        <Col xs={24} lg={6}>
          <Card
            title="DingTalk"
            extra={renderStatusBadge(channelMap.dingtalk?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'dingtalk' ? null : 'dingtalk')
            }
          >
            {expanded === 'dingtalk' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={dingtalkForm}
                  layout="vertical"
                  onFinish={handleSaveDingtalk}
                >
                  <Form.Item
                    name="clientId"
                    label="Client ID (AppKey)"
                    rules={[{ required: true }]}
                  >
                    <Input placeholder="ding..." />
                  </Form.Item>
                  <Form.Item
                    name="clientSecret"
                    label="Client Secret (AppSecret)"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Client secret" />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'dingtalk'}
                    >
                      Save
                    </Button>
                    <Popconfirm
                      title="Clear DingTalk credentials?"
                      onConfirm={() => handleClear('dingtalk')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
                <Text
                  type="secondary"
                  style={{ display: 'block', marginTop: 12, fontSize: 12 }}
                >
                  DingTalk bots use Stream mode (WebSocket). Configure the bot
                  in the DingTalk developer console, then enter the credentials
                  here or add it as a multi-bot below.
                </Text>
              </div>
            )}
          </Card>
        </Col>
      </Row>

      {/* Multi-Bot WS Bridge */}
      <Divider />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <Title level={4} style={{ margin: 0 }}>
            <RobotOutlined style={{ marginRight: 8 }} />
            Multi-Bot Bridge (WebSocket)
          </Title>
          <Text type="secondary">
            DingTalk Stream + Feishu WebSocket bots managed by WS Bridge (ECS Fargate)
          </Text>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setAddBotOpen(true)}
        >
          Add Bot
        </Button>
      </div>

      <Table
        columns={botColumns}
        dataSource={bots}
        rowKey="id"
        loading={botsLoading}
        pagination={false}
        size="middle"
        locale={{ emptyText: 'No bots configured. Click "Add Bot" to get started.' }}
      />

      {/* Add Bot Modal */}
      <Modal
        title="Add Bot"
        open={addBotOpen}
        onCancel={() => {
          setAddBotOpen(false);
          addBotForm.resetFields();
        }}
        footer={null}
      >
        <Form form={addBotForm} layout="vertical" onFinish={handleAddBot}>
          <Form.Item
            name="id"
            label="Bot ID"
            rules={[
              { required: true, message: 'Enter a unique bot ID' },
              {
                pattern: /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$/,
                message: 'Alphanumeric, hyphens, underscores (1-48 chars)',
              },
            ]}
          >
            <Input placeholder="e.g. dingtalk-main, feishu-team-a" />
          </Form.Item>
          <Form.Item
            name="channel"
            label="Channel"
            rules={[{ required: true }]}
          >
            <Select placeholder="Select channel type">
              <Select.Option value="dingtalk">DingTalk</Select.Option>
              <Select.Option value="feishu">Feishu</Select.Option>
            </Select>
          </Form.Item>

          {addBotChannel === 'dingtalk' && (
            <>
              <Form.Item
                name="clientId"
                label="Client ID (AppKey)"
                rules={[{ required: true }]}
              >
                <Input placeholder="ding..." />
              </Form.Item>
              <Form.Item
                name="clientSecret"
                label="Client Secret (AppSecret)"
                rules={[{ required: true }]}
              >
                <Input.Password placeholder="Client secret" />
              </Form.Item>
            </>
          )}

          {addBotChannel === 'feishu' && (
            <>
              <Form.Item
                name="appId"
                label="App ID"
                rules={[{ required: true }]}
              >
                <Input placeholder="cli_..." />
              </Form.Item>
              <Form.Item
                name="appSecret"
                label="App Secret"
                rules={[{ required: true }]}
              >
                <Input.Password placeholder="App secret" />
              </Form.Item>
            </>
          )}

          <Space>
            <Button type="primary" htmlType="submit">
              Add
            </Button>
            <Button
              onClick={() => {
                setAddBotOpen(false);
                addBotForm.resetFields();
              }}
            >
              Cancel
            </Button>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
