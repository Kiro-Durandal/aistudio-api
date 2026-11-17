// Package netresolve configures DNS for the Linux static signer running in Termux.
package netresolve

import (
	"context"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

// Configure uses an explicit DNS server or Termux's configured nameservers.
// With neither configuration, the operating system resolver is left untouched.
// Call it before starting any network workers.
func Configure(server, termuxPrefix string) (string, error) {
	var servers []string
	source := "AISTUDIO_DNS_SERVER / -dns"
	if server = strings.TrimSpace(server); server != "" {
		address, err := dnsAddress(server)
		if err != nil {
			return "", err
		}
		servers = append(servers, address)
	} else if termuxPrefix != "" {
		source = filepath.Join(termuxPrefix, "etc", "resolv.conf")
		content, err := os.ReadFile(source)
		if os.IsNotExist(err) {
			return "", nil
		}
		if err != nil {
			return "", fmt.Errorf("读取 Termux DNS 配置: %w", err)
		}
		for _, line := range strings.Split(string(content), "\n") {
			fields := strings.Fields(line)
			if len(fields) < 2 || fields[0] != "nameserver" {
				continue
			}
			if address, err := dnsAddress(fields[1]); err == nil {
				servers = append(servers, address)
			}
		}
		if len(servers) == 0 {
			return "", fmt.Errorf("%s 中没有有效 nameserver，请配置 DNS 或设置 AISTUDIO_DNS_SERVER", source)
		}
	}
	if len(servers) == 0 {
		return "", nil
	}

	var next atomic.Uint64
	dialer := &net.Dialer{Timeout: 5 * time.Second}
	net.DefaultResolver = &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			// Rotate configured servers across queries and retries; preserve TCP
			// fallback when a DNS response does not fit in UDP.
			address := servers[(next.Add(1)-1)%uint64(len(servers))]
			return dialer.DialContext(ctx, network, address)
		},
	}
	return source, nil
}

func dnsAddress(server string) (string, error) {
	if ip, err := netip.ParseAddr(server); err == nil {
		return netip.AddrPortFrom(ip, 53).String(), nil
	}
	if address, err := netip.ParseAddrPort(server); err == nil && address.Port() != 0 {
		return address.String(), nil
	}
	return "", fmt.Errorf("DNS 地址必须为 IP 或 IP:端口，不能是域名或网址: %q", server)
}
