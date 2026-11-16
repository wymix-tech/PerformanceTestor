@Mapper
public interface OrderMapper {
    java.util.List<com.demo.Order> listOrders();
    com.demo.Order getOrder(Long id);
    void insertOrder(com.demo.Order o);
}
