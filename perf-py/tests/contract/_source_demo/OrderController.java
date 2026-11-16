/**
 * @RestController
 */
@RestController
@RequestMapping("/api/order")
public class OrderController {
    private final OrderMapper orderMapper;
    @GetMapping("/list")
    public java.util.List<com.demo.Order> list() {
        return orderMapper.listOrders();
    }
    @GetMapping("/{id}")
    public com.demo.Order detail(Long id) {
        return orderMapper.getOrder(id);
    }
    @PostMapping("/create")
    public void create(com.demo.Order o) {
        orderMapper.insertOrder(o);
    }
}
